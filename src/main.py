# main.py
#
# Copyright 2026 Golodnikov Sergey
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later


from datetime import timedelta
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import data
from .window import WindowIF


TMP_NAME = 'result'


class ImageFlowApplication(Adw.Application):
    def __init__(self, version=''):
        super().__init__(application_id='tech.digiroad.ImageFlow',
                         flags=Gio.ApplicationFlags.HANDLES_OPEN,
                         resource_base_path='/tech/digiroad/ImageFlow')
        self.version = version
        self.create_action('open', self.open_file, ['<primary>o'])
        self.create_action('about', self.about_action, None)
        self.create_action('quit', lambda *_: self.quit(), ['<control>q'])
        self.create_action('preferences',
                           self.preferences_action,
                           ['<primary>p'])
        self.signal_handlers_connected = False

    def do_activate(self):
        self.settings = Gio.Settings.new('tech.digiroad.ImageFlow')

        self.options = {}
        self.options_exceptions = (
            'theme',
            'theme-user',
            'detect-size',
            'loop',
            'accurate-rnd',
            'stats-mode',
            'bayer-scale',
            'webp-lossless',
            'webp-quality',
            'webp-preset',
            'avif-quality',
            'avif-speed',
        )
        self.options_load()

        self.w = self.props.active_window
        if not self.w:
            self.w = WindowIF(application=self)
        self.w.present()

        user_theme = self.settings.get_boolean('theme-user')
        if not user_theme:
            theme = self.get_system_color_scheme()
            self.settings.set_int('theme', theme)
            self.update_theme(theme)
        else:
            self.update_theme(self.settings.get_int('theme'))

        loop_state = self.settings.get_boolean('loop')
        self.w.loop.set_active(loop_state)
        self.w.video.set_loop(loop_state)

        self.options_set()

        self.source, self.result, self.current = '', '', ''

        self.name, self.file_format = '', ''

        self.dir = GLib.get_user_cache_dir()
        os.makedirs(self.dir, exist_ok=True)
        self.palette = os.path.join(self.dir, 'palette.png')
        self.sources_size = None
        self.source_duration_us = 0
        self.crop_rect = None
        self.crop_drag_mode = None
        self.crop_drag_start = None
        self.crop_drag_start_rect = None
        self.frame_exclusions = []
        self.frame_selection = set()
        self.frame_buttons = {}
        self.frame_generation = 0
        self.frame_page_start = 0
        self.frame_total_count = 0
        self.frame_timeline_fps = 0
        self.generating = False
        self.frame_temp = tempfile.TemporaryDirectory(
            prefix='imageflow-frames-')
        self.frame_process = None
        self.frame_process_lock = threading.Lock()

        self.stream = None
        self.stream_timestamp_handler = None
        self.enable_trim = False
        self.segment_point = 0  # 1:start, 2:end
        self.segment_format = 'h'
        self.segment_format_options = data.time_format_options['h']
        self.segment_value_start = 0
        self.segment_value_end = 0

        self.freeze = False

        if not self.signal_handlers_connected:
            self.signal_handlers_connected = True
            self.connect_signals()
        # format check
        self.format_switch(self.w.format, None)

    def do_open(self, files, n_files, _hint):
        self.activate()
        for file in files[:n_files]:
            path = file.get_path()
            if path is not None:
                self.accept_file(path)
                break

    # -------------------------------------------------------------------------

    def connect_signals(self):
        self.w.pref_theme.connect('notify::selected-item', self.theme_change)
        self.w.pref_dialog.connect('closed', self.preferences_save)

        self.w.external.connect('clicked', self.browser_preview)
        self.w.generate.connect('activated', self.generate_wrapper)
        self.w.image_height.connect('notify::value', self.size_change)
        self.w.image_size.connect('notify::selected-item', self.size_switch)
        self.w.image_width.connect('notify::value', self.size_change)
        self.w.keep_aspect_ratio.connect('notify::active', self.ratio_state)
        self.w.loop.connect('toggled', self.loop_state)
        self.w.open_file.connect('clicked', self.open_file)
        self.w.preview.connect('notify::active', self.preview_switch)
        self.w.save_file.connect('activated', self.save_file)
        self.w.trim.connect('toggled', self.trim_state)
        self.w.crop_toggle.connect(
            'notify::active', self.crop_toggle_state)
        self.w.crop_reset.connect('clicked', self.crop_reset)
        self.w.frames_toggle.connect(
            'notify::active', self.frames_toggle_state)
        self.w.frames_page_previous.connect(
            'clicked', self.frames_page_previous)
        self.w.frames_page_next.connect('clicked', self.frames_page_next)
        self.w.frames_remove.connect('clicked', self.frames_remove)
        self.w.frames_clear.connect('clicked', self.frames_clear)
        self.w.framerate.connect('notify::value', self.framerate_changed)

        # trim, segment
        self.w.segment_button_start.connect(
            'clicked', self.segment_button_start)
        self.w.segment_button_end.connect(
            'clicked', self.segment_button_end)
        self.w.segment_entry_start.connect(
            'activate', self.segment_entry_start)
        self.w.segment_entry_end.connect(
            'activate', self.segment_entry_end)
        # format
        self.w.format.connect('notify::selected-item', self.format_switch)
        # drag & drop
        drop_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop_target.connect('drop', self.on_drop)
        self.w.display.add_controller(drop_target)
        crop_drag = Gtk.GestureDrag.new()
        crop_drag.connect('drag-begin', self.crop_drag_begin)
        crop_drag.connect('drag-update', self.crop_drag_update)
        crop_drag.connect('drag-end', self.crop_drag_end)
        self.w.crop_canvas.add_controller(crop_drag)
        self.w.crop_canvas.set_draw_func(self.draw_crop_overlay)

    # -------------------------------------------------------------------------

    def switch_control(self, generate: bool, preview: bool, save: bool):
        # generate
        self.w.generate.set_sensitive(generate)
        if generate:
            self.w.generate.add_css_class('warning')
        else:
            self.w.generate.remove_css_class('warning')
        # preview
        self.w.preview.set_sensitive(preview)
        if preview:
            self.w.preview.add_css_class('success')
        else:
            self.w.preview.remove_css_class('success')
        self.w.preview.set_active(preview)
        # save
        self.w.save_file.set_sensitive(save)
        if save:
            self.w.save_file.add_css_class('suggested-action')
        else:
            self.w.save_file.remove_css_class('suggested-action')

    # -------------------------------------------------------------------------

    def accept_file(self, path: str):
        self.result, self.name = '', ''
        self.source = path
        self.sources_size = None
        self.source_duration_us = 0
        self.crop_rect = None
        self.frame_exclusions = []
        self.frame_selection.clear()
        self.frame_page_start = 0
        self.frame_total_count = 0
        self.frame_timeline_fps = 0
        self.frame_generation += 1
        self.cancel_frame_extraction()
        self.clear_frame_buttons()
        self.w.frames_toggle.set_active(False)
        self.w.frames_toggle.set_sensitive(False)
        self.w.frames_remove.set_sensitive(False)
        self.w.frames_clear.set_sensitive(False)
        self.w.crop_toggle.set_active(False)
        self.w.crop_toggle.set_sensitive(False)
        self.w.crop_reset.set_sensitive(False)
        self.update_crop_overlay()
        self.w.video.set_filename(self.source)
        self.current = self.source
        self.w.open_file.remove_css_class('suggested-action')
        self.w.title.set_subtitle(os.path.basename(self.source))
        self.switch_control(generate=True, preview=False, save=False)
        self.file_parsing()
        # stream & trim
        self.trim_access(self.connect_stream())

    def clear_frame_buttons(self):
        self.frame_buttons.clear()
        child = self.w.frames_flow.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.w.frames_flow.remove(child)
            child = next_child

    def connect_stream(self) -> bool:
        self.disconnect_stream()
        self.stream = self.w.video.get_media_stream()
        if self.stream is None:
            return False
        self.stream_timestamp_handler = self.stream.connect(
            'notify::timestamp', self.get_timestamp)
        return True

    def disconnect_stream(self):
        if self.stream is not None and self.stream_timestamp_handler:
            self.stream.disconnect(self.stream_timestamp_handler)
        self.stream_timestamp_handler = None

    def on_drop(self, _drop, value, _x, _y):
        if not value:
            return False
        path = value.get_path()
        if not os.path.exists(path):
            self.message_show(*self.w.ts_error_permissions)
            return False
        else:
            self.accept_file(path)
            return True

    def file_parsing(self):
        result = subprocess.run([
            'ffprobe', '-v', 'error',
            '-select_streams', 'v',
            '-show_entries', 'stream=width,height',
            '-show_entries', 'format=duration',
            '-of', 'csv=p=0', self.source
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            err = result.stderr.decode('utf-8').strip()
            self.message_show(self.w.ts_error_analysis, err)
            return
        lines = result.stdout.decode('utf-8').strip().split('\n')
        if len(lines) < 2:
            if any(',' in line for line in lines):
                detail = self.w.ts_error_analysis_duration
            else:
                detail = self.w.ts_error_analysis_video
            self.message_show(self.w.ts_error_analysis, detail)
            return
        size, duration = lines[0], lines[1]
        # duration
        try:
            duration = int(float(duration) * 1000000)
            self.source_duration_us = duration
            self.segment_range_set(duration, init=True)
            self.load_frame_thumbnails()
        except ValueError as err:
            self.message_show(self.w.ts_error_analysis, str(err))
        # size
        split = size.split(',')
        if len(split) == 2:
            try:
                width, height = int(split[0]), int(split[1])
                self.sources_size = (width, height)
                self.w.crop_canvas.queue_draw()
                if self.settings.get_boolean('detect-size'):
                    self.freeze = True
                    self.w.image_width.set_value(width)
                    self.w.image_height.set_value(height)
                    self.freeze = False
                    self.w.image_size.set_selected(0)
                elif self.w.image_size.get_selected() == 0:
                    self.freeze = True
                    self.w.image_width.set_value(width)
                    self.w.image_height.set_value(height)
                    self.freeze = False
            except ValueError as err:
                self.message_show(self.w.ts_error_analysis, str(err))
        self.w.crop_toggle.set_sensitive(self.sources_size is not None)

    def load_frame_thumbnails(self):
        if self.source == '' or self.source_duration_us <= 0:
            return

        fps = max(1, int(self.w.framerate.get_value()))
        if self.frame_timeline_fps and self.frame_timeline_fps != fps:
            previous_fps = self.frame_timeline_fps
            self.frame_exclusions = self.merge_frame_ranges([
                (round(start * fps / previous_fps),
                 round(end * fps / previous_fps))
                for start, end in self.frame_exclusions
            ])
            self.frame_selection = {
                round(index * fps / previous_fps)
                for index in self.frame_selection
            }
            self.frame_page_start = 0
        self.frame_timeline_fps = fps
        total_frames = max(
            1, int((self.source_duration_us / 1000000) * fps + 0.5))
        self.frame_total_count = total_frames
        self.frame_page_start = min(
            self.frame_page_start, max(0, total_frames - 1))
        indexes = list(range(
            self.frame_page_start,
            min(total_frames, self.frame_page_start + 48),
        ))

        self.frame_generation += 1
        generation = self.frame_generation
        self.cancel_frame_extraction()
        threading.Thread(
            target=self.extract_frame_thumbnails,
            args=(generation, self.source, fps, indexes),
            daemon=True,
        ).start()

    def extract_frame_thumbnails(self, generation, source, fps, indexes):
        output_dir = os.path.join(self.frame_temp.name, str(generation))
        try:
            os.makedirs(output_dir, exist_ok=True)
            selection = '+'.join(f'eq(n\\,{index})' for index in indexes)
            filter_chain = f'fps={fps},select={selection},scale=144:-2'
            process = subprocess.Popen([
                'ffmpeg', '-v', 'error', '-i', source,
                '-vf', filter_chain, '-fps_mode', 'vfr',
                '-frames:v', str(len(indexes)),
                os.path.join(output_dir, 'frame-%03d.jpg'),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as err:
            GLib.idle_add(
                self.frame_thumbnails_failed, generation, str(err))
            return
        with self.frame_process_lock:
            cancelled = generation != self.frame_generation
            if not cancelled:
                self.frame_process = process
        if cancelled:
            self.terminate_frame_process(process)
        _, error = process.communicate()
        with self.frame_process_lock:
            if self.frame_process is process:
                self.frame_process = None
        if process.returncode != 0:
            detail = error.decode('utf-8').strip()
            GLib.idle_add(
                self.frame_thumbnails_failed, generation, detail)
            return
        paths = [
            os.path.join(output_dir, f'frame-{number:03d}.jpg')
            for number in range(1, len(indexes) + 1)
        ]
        GLib.idle_add(
            self.frame_thumbnails_loaded, generation, fps, indexes, paths)

    def terminate_frame_process(self, process):
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    def cancel_frame_extraction(self):
        with self.frame_process_lock:
            process = self.frame_process
            self.frame_process = None
        self.terminate_frame_process(process)

    def frame_thumbnails_failed(self, generation, detail):
        if generation == self.frame_generation:
            self.message_show(self.w.ts_error_analysis, detail)
        return GLib.SOURCE_REMOVE

    def frame_thumbnails_loaded(self, generation, fps, indexes, paths):
        if generation != self.frame_generation:
            return GLib.SOURCE_REMOVE
        self.clear_frame_buttons()
        for index, path in zip(indexes, paths):
            if not os.path.isfile(path):
                continue
            picture = Gtk.Picture.new_for_filename(path)
            picture.set_size_request(128, 72)
            picture.set_can_shrink(True)
            label = Gtk.Label(label=self.microseconds_to_hms(
                round(index * 1000000 / fps)))
            card = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.append(picture)
            card.append(label)
            button = Gtk.ToggleButton(child=card)
            button.set_tooltip_text(
                f'{self.w.ts_frame} {index + 1} — {label.get_text()}')
            if self.frame_is_excluded(index):
                button.add_css_class('destructive-action')
            button.connect('toggled', self.frame_selection_changed, index)
            self.w.frames_flow.append(button)
            self.frame_buttons[index] = button
            button.set_active(index in self.frame_selection)
        self.w.frames_toggle.set_sensitive(
            bool(self.frame_buttons) and not self.generating)
        if self.frame_buttons:
            self.w.frames_toggle.set_active(True)
        self.w.frames_clear.set_sensitive(bool(self.frame_exclusions))
        self.update_frames_pager()
        self.update_frames_controls()
        return GLib.SOURCE_REMOVE

    def frame_is_excluded(self, index):
        return any(start <= index <= end
                   for start, end in self.frame_exclusions)

    def frame_selection_changed(self, button, index):
        if button.get_active():
            self.frame_selection.add(index)
        else:
            self.frame_selection.discard(index)
        self.update_frames_controls()

    def update_frames_controls(self):
        editable = not self.generating and (
            self.result == '' or self.current == self.source)
        for button in self.frame_buttons.values():
            button.set_sensitive(editable)
        self.w.frames_remove.set_sensitive(
            bool(self.frame_selection) and editable)
        self.w.frames_clear.set_sensitive(
            bool(self.frame_exclusions) and editable)

    def frames_toggle_state(self, widget, _):
        self.w.frames_revealer.set_reveal_child(widget.get_active())

    def frames_page_previous(self, _button):
        self.frame_page_start = max(0, self.frame_page_start - 48)
        self.load_frame_thumbnails()

    def frames_page_next(self, _button):
        if self.frame_page_start + 48 < self.frame_total_count:
            self.frame_page_start += 48
            self.load_frame_thumbnails()

    def update_frames_pager(self):
        if self.frame_total_count:
            first = self.frame_page_start + 1
            last = min(self.frame_total_count, self.frame_page_start + 48)
            self.w.frames_page_label.set_text(
                f'{first}–{last} / {self.frame_total_count}')
        else:
            self.w.frames_page_label.set_text('')
        self.w.frames_page_previous.set_sensitive(self.frame_page_start > 0)
        self.w.frames_page_next.set_sensitive(
            self.frame_page_start + 48 < self.frame_total_count)

    def frames_remove(self, _button):
        if not self.frame_selection:
            return
        selected = sorted(self.frame_selection)
        self.frame_exclusions.append((selected[0], selected[-1]))
        self.frame_exclusions = self.merge_frame_ranges(
            self.frame_exclusions)
        self.frame_selection.clear()
        for index, button in self.frame_buttons.items():
            button.set_active(False)
            if self.frame_is_excluded(index):
                button.add_css_class('destructive-action')
            else:
                button.remove_css_class('destructive-action')
        self.update_frames_controls()

    @staticmethod
    def merge_frame_ranges(ranges):
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def frames_clear(self, _button):
        self.frame_exclusions.clear()
        for button in self.frame_buttons.values():
            button.remove_css_class('destructive-action')
        self.update_frames_controls()

    def framerate_changed(self, _widget, _):
        if self.source != '' and self.source_duration_us > 0:
            self.load_frame_thumbnails()

    def frame_exclusions_for_trim(self):
        fps = max(1, int(self.w.framerate.get_value()))
        trim_start = (
            round(self.segment_value_start * fps / 1000000)
            if self.enable_trim else 0
        )
        return [
            (max(0, start - trim_start), end - trim_start)
            for start, end in self.frame_exclusions
            if end >= trim_start
        ]

    def all_output_frames_excluded(self):
        fps = max(1, int(self.w.framerate.get_value()))
        start = self.segment_value_start if self.enable_trim else 0
        end = self.segment_value_end if self.enable_trim \
            else self.source_duration_us
        frame_count = max(1, int((end - start) * fps / 1000000 + 0.5))
        ranges = self.frame_exclusions_for_trim()
        return bool(ranges) and ranges[0][0] == 0 \
            and ranges[0][1] >= frame_count - 1

    def crop_display_rect(self, width, height):
        if not self.sources_size:
            return None
        source_width, source_height = self.sources_size
        aspect = source_width / source_height
        if width / height > aspect:
            display_height = height
            display_width = height * aspect
            left = (width - display_width) / 2
            top = 0
        else:
            display_width = width
            display_height = width / aspect
            left = 0
            top = (height - display_height) / 2
        return left, top, display_width, display_height

    def crop_to_canvas(self, x, y):
        width, height = self.w.crop_canvas.get_width(), \
            self.w.crop_canvas.get_height()
        display = self.crop_display_rect(width, height)
        if display is None:
            return 0, 0
        left, top, display_width, display_height = display
        source_width, source_height = self.sources_size
        return (
            left + x / source_width * display_width,
            top + y / source_height * display_height,
        )

    def crop_to_source(self, x, y):
        width, height = self.w.crop_canvas.get_width(), \
            self.w.crop_canvas.get_height()
        display = self.crop_display_rect(width, height)
        if display is None:
            return 0, 0
        left, top, display_width, display_height = display
        source_width, source_height = self.sources_size
        return (
            min(source_width, max(0, (x - left) / display_width
                                  * source_width)),
            min(source_height, max(0, (y - top) / display_height
                                   * source_height)),
        )

    def crop_handle_points(self, rect):
        x, y, width, height = rect
        return {
            'nw': (x, y), 'n': (x + width / 2, y),
            'ne': (x + width, y), 'e': (x + width, y + height / 2),
            'se': (x + width, y + height), 's': (x + width / 2, y + height),
            'sw': (x, y + height), 'w': (x, y + height / 2),
        }

    def draw_crop_overlay(
            self, _area, context, width, height, _user_data=None):
        if not self.sources_size or not self.crop_rect:
            return
        display = self.crop_display_rect(width, height)
        if display is None:
            return
        left, top, display_width, display_height = display
        source_width, source_height = self.sources_size
        x, y, crop_width, crop_height = self.crop_rect
        x = left + x / source_width * display_width
        y = top + y / source_height * display_height
        crop_width = crop_width / source_width * display_width
        crop_height = crop_height / source_height * display_height
        context.set_source_rgba(0.35, 0.85, 0.55, 0.95)
        context.set_line_width(2)
        context.rectangle(x, y, crop_width, crop_height)
        context.stroke()
        context.set_source_rgba(0.15, 0.75, 0.4, 0.95)
        for point_x, point_y in self.crop_handle_points(self.crop_rect).values():
            canvas_x = left + point_x / source_width * display_width
            canvas_y = top + point_y / source_height * display_height
            context.arc(canvas_x, canvas_y, 5, 0, 6.283185307179586)
            context.fill()

    def crop_toggle_state(self, _widget, _):
        self.update_crop_overlay()

    def update_crop_overlay(self):
        if not hasattr(self, 'w'):
            return
        source_visible = self.source != '' and self.current == self.source \
            and self.w.display.get_visible()
        visible = source_visible and (
            self.w.crop_toggle.get_active() or self.crop_rect is not None)
        self.w.crop_canvas.set_visible(visible)
        self.w.crop_canvas.set_can_target(
            source_visible and self.w.crop_toggle.get_active())
        self.w.crop_canvas.queue_draw()

    def crop_drag_begin(self, _gesture, x, y):
        if not self.sources_size:
            return
        self.crop_drag_start = self.crop_to_source(x, y)
        self.crop_drag_start_canvas = (x, y)
        self.crop_drag_start_rect = self.crop_rect
        self.crop_drag_mode = 'new'
        if self.crop_rect:
            handles = self.crop_handle_points(self.crop_rect)
            for name, point in handles.items():
                canvas_point = self.crop_to_canvas(*point)
                if (canvas_point[0] - x) ** 2 + (canvas_point[1] - y) ** 2 \
                        <= 20 ** 2:
                    self.crop_drag_mode = name
                    break
            else:
                crop_x, crop_y, crop_width, crop_height = self.crop_rect
                point_x, point_y = self.crop_drag_start
                if crop_x <= point_x <= crop_x + crop_width \
                        and crop_y <= point_y <= crop_y + crop_height:
                    self.crop_drag_mode = 'move'
        if self.crop_drag_mode == 'new':
            self.crop_rect = (*self.crop_drag_start, 1, 1)
        self.w.crop_canvas.queue_draw()

    def crop_drag_update(self, _gesture, offset_x, offset_y):
        if self.crop_drag_start is None:
            return
        start_x, start_y = self.crop_drag_start
        canvas_start_x, canvas_start_y = self.crop_drag_start_canvas
        point_x, point_y = self.crop_to_source(
            canvas_start_x + offset_x, canvas_start_y + offset_y)
        source_width, source_height = self.sources_size
        if self.crop_drag_mode == 'new':
            self.crop_rect = (
                min(start_x, point_x), min(start_y, point_y),
                abs(point_x - start_x), abs(point_y - start_y),
            )
        elif self.crop_drag_mode == 'move':
            old_x, old_y, old_width, old_height = self.crop_drag_start_rect
            x = min(max(0, old_x + point_x - start_x),
                    source_width - old_width)
            y = min(max(0, old_y + point_y - start_y),
                    source_height - old_height)
            self.crop_rect = (x, y, old_width, old_height)
        else:
            old_x, old_y, old_width, old_height = self.crop_drag_start_rect
            edges = {
                'left': old_x, 'top': old_y,
                'right': old_x + old_width, 'bottom': old_y + old_height,
            }
            if 'w' in self.crop_drag_mode:
                edges['left'] = min(point_x, edges['right'] - 16)
            if 'e' in self.crop_drag_mode:
                edges['right'] = max(point_x, edges['left'] + 16)
            if 'n' in self.crop_drag_mode:
                edges['top'] = min(point_y, edges['bottom'] - 16)
            if 's' in self.crop_drag_mode:
                edges['bottom'] = max(point_y, edges['top'] + 16)
            edges['left'] = max(0, edges['left'])
            edges['top'] = max(0, edges['top'])
            edges['right'] = min(source_width, edges['right'])
            edges['bottom'] = min(source_height, edges['bottom'])
            self.crop_rect = (
                edges['left'], edges['top'],
                edges['right'] - edges['left'],
                edges['bottom'] - edges['top'],
            )
        self.w.crop_reset.set_sensitive(True)
        self.w.crop_canvas.queue_draw()

    def crop_drag_end(self, _gesture, _offset_x, _offset_y):
        if self.crop_rect:
            x, y, width, height = self.crop_rect
            if width < 16 or height < 16:
                self.crop_rect = None
                self.w.crop_reset.set_sensitive(False)
            else:
                self.crop_rect = tuple(round(value) for value in
                                       (x, y, width, height))
        self.crop_drag_start = None
        self.crop_drag_start_rect = None
        self.crop_drag_mode = None
        self.w.crop_canvas.queue_draw()

    def crop_reset(self, _button):
        self.crop_rect = None
        self.w.crop_reset.set_sensitive(False)
        self.w.crop_canvas.queue_draw()

    def crop_filter(self):
        if not self.crop_rect or not self.sources_size:
            return ''
        x, y, width, height = self.crop_rect
        source_width, source_height = self.sources_size
        x = max(0, min(source_width - 2, round(x))) & ~1
        y = max(0, min(source_height - 2, round(y))) & ~1
        width = max(2, round(width)) & ~1
        height = max(2, round(height)) & ~1
        width = min(width, (source_width - x) & ~1)
        height = min(height, (source_height - y) & ~1)
        if width < 2 or height < 2:
            return ''
        return f'crop={width}:{height}:{x}:{y},'

    # -------------------------------------------------------------------------

    def open_file(self, _button, _=None):
        def open_file_finish(dialog, result):
            try:
                file = dialog.open_finish(result)
            except GLib.Error as err:
                if err.matches(Gtk.DialogError.quark(),
                               Gtk.DialogError.DISMISSED):
                    return
                self.message_show(self.w.ts_error_analysis, str(err))
                return
            if not file:
                return
            self.accept_file(file.get_path())

        dialog = Gtk.FileDialog.new()
        dialog.set_title('Select a video file')
        # file type filters
        filters = Gio.ListStore.new(Gtk.FileFilter)
        # all files
        filter = Gtk.FileFilter.new()
        filter.set_name('All')
        filter.add_pattern('*.*')
        filters.append(filter)
        # video files
        filter = Gtk.FileFilter.new()
        filter.set_name('Video')

        types = (
            'video/mp4',
            'video/mpeg',
            'video/ogg',
            'video/quicktime',
            'video/webm',
            'video/x-matroska',
        )
        for t in types:
            filter.add_mime_type(t)

        extensions = (
            '*.avi',
            '*.mkv',
            '*.mov',
            '*.mp4',
            '*.mpeg',
            '*.mpg',
            '*.webm',
        )
        for e in extensions:
            filter.add_pattern(e)

        filters.append(filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(filter)
        dialog.open(self.w, None, open_file_finish)

    def save_file(self, _):
        def save_file_finish(dialog, result):
            try:
                file = dialog.save_finish(result)
            except Exception as err:
                err = str(err)
                if 'dismissed by user' not in err.lower():
                    self.message_show(self.w.ts_error_saving, err)
                return
            if not file:
                return
            fp = file.get_path()
            if not fp:
                return
            try:
                shutil.copy2(self.result, fp)
            except OSError as err:
                self.message_show(self.w.ts_error_saving, str(err))
                return
            toast_title = f'{self.w.ts_save} {self.name}'
            toast = Adw.Toast.new(title=toast_title)
            toast.set_button_label(button_label=self.w.ts_save_show)
            toast.connect('button-clicked',
                          lambda s: self.toast_button_show(s, fp))
            self.w.overlay.add_toast(toast)

        dialog = Gtk.FileDialog.new()
        dialog.set_title('Save result')
        dialog.set_initial_name(self.name)
        # file type filters
        filters = Gio.ListStore.new(Gtk.FileFilter)
        default_filter = None
        for title, extension in (('GIF', 'gif'), ('WebP', 'webp'),
                                 ('AVIF', 'avif')):
            f = Gtk.FileFilter()
            f.set_name(title)
            f.add_pattern(f'*.{extension}')
            filters.append(f)
            if f'.{extension}' == self.file_format:
                default_filter = f
        dialog.set_filters(filters)
        if default_filter is not None:
            dialog.set_default_filter(default_filter)
        dialog.save(self.w, None, save_file_finish)

    # -------------------------------------------------------------------------

    def stack_adjust_visibility(self, obj: str):
        match obj:
            case 'display':
                self.w.display.set_visible(True)
                self.w.spinner.set_visible(False)
                self.w.external.set_visible(False)
                self.update_crop_overlay()
            case 'spinner':
                self.w.display.set_visible(False)
                self.w.spinner.set_visible(True)
                self.w.external.set_visible(False)
                self.update_crop_overlay()
            case 'external':
                self.w.display.set_visible(False)
                self.w.spinner.set_visible(False)
                self.w.external.set_visible(True)
                self.update_crop_overlay()

    def format_switch(self, widget, _):
        self.file_format = data.format[widget.get_selected()]
        v = True if self.file_format == '.gif' else False
        self.w.max_colors.set_sensitive(v)
        self.w.dither.set_sensitive(v)

    def size_switch(self, widget, _):
        self.freeze = True
        size = data.size[widget.get_selected()]
        if size == 'Original':
            if self.sources_size is not None:
                self.w.image_width.set_value(self.sources_size[0])
                self.w.image_height.set_value(self.sources_size[1])
        elif size == 'User':
            pass
        else:
            self.w.image_width.set_value(size[0])
            self.w.image_height.set_value(size[1])
        self.freeze = False

    def size_change(self, _widget, _):
        if not self.freeze:
            self.w.image_size.set_selected(len(data.size) - 1)

    def ratio_state(self, widget, _):
        state = widget.get_active()
        self.w.image_height.set_sensitive(False if state else True)

    def loop_state(self, toggle_button):
        state = toggle_button.get_active()
        self.settings.set_boolean('loop', state)
        self.w.video.set_loop(state)
        if self.current != '':
            self.w.video.set_filename(self.current)
            self.connect_stream()

    # -------------------------------------------------------------------------

    def trim_state(self, toggle_button):
        if self.freeze:
            return
        self.segment_point = 0
        state = toggle_button.get_active()
        if not state:
            self.enable_trim = False
            self.w.segment.set_visible(False)
            self.w.segment_box_start.remove_css_class('success')
            self.w.segment_box_end.remove_css_class('success')
            return
        if self.source == '' or self.source != self.current \
                or self.stream is None:
            # trim unavailable: restore switch to real state
            self.freeze = True
            self.w.trim.set_active(False)
            self.freeze = False
            return
        self.enable_trim = True
        self.w.segment.set_visible(True)
        duration = self.stream.get_duration()
        if duration != 0:
            self.segment_range_set(duration)

    def trim_access(self, access: bool):
        self.freeze = True
        if access:
            self.w.trim.set_sensitive(True)
        else:
            self.segment_point = 0
            self.enable_trim = False
            self.w.segment.set_visible(False)
            self.w.segment_box_start.remove_css_class('success')
            self.w.segment_box_end.remove_css_class('success')
            self.w.trim.set_active(False)
            self.w.trim.set_sensitive(False)
        self.freeze = False

    # -------------------------------------------------------------------------

    def get_timestamp(self, _f, _p):
        if self.segment_point != 0:
            self.segment_range_set(self.stream.get_timestamp())

    def segment_button_start(self, _):
        self.enable_trim = True
        if self.segment_point == 1:  # start
            self.segment_point = 0
            self.w.segment_box_start.remove_css_class('success')
        else:
            self.segment_point = 1
            self.w.segment_box_start.add_css_class('success')
            self.w.segment_box_end.remove_css_class('success')
            if self.stream is not None \
                    and self.segment_value_start == 0:
                self.segment_range_set(self.stream.get_timestamp())

    def segment_button_end(self, _):
        self.enable_trim = True
        if self.segment_point == 2:  # end
            self.segment_point = 0
            self.w.segment_box_end.remove_css_class('success')
        else:
            self.segment_point = 2
            self.w.segment_box_start.remove_css_class('success')
            self.w.segment_box_end.add_css_class('success')

    def segment_entry_start(self, entry):
        if self.stream is None:
            return
        text = entry.get_text()
        if re.fullmatch(self.segment_format_options[1], text):
            entry.remove_css_class('error')
            try:
                microseconds = self.text_to_microseconds(text)
            except ValueError as err:
                self.message_show(self.w.ts_error_timestamp, str(err))
                return
            self.stream.seek(microseconds)
            self.segment_value_start = microseconds
            # disarm live capture so the typed value is not overwritten
            if self.segment_point == 1:
                self.segment_point = 0
            self.w.segment_box_start.add_css_class('success')
        else:
            entry.add_css_class('error')

    def segment_entry_end(self, entry):
        if self.stream is None:
            return
        text = entry.get_text()
        if re.fullmatch(self.segment_format_options[1], text):
            entry.remove_css_class('error')
            try:
                microseconds = self.text_to_microseconds(text)
            except ValueError as err:
                self.message_show(self.w.ts_error_timestamp, str(err))
                return
            self.stream.seek(microseconds)
            self.segment_value_end = microseconds
            # disarm live capture so the typed value is not overwritten
            if self.segment_point == 2:
                self.segment_point = 0
            self.w.segment_box_end.add_css_class('success')
        else:
            entry.add_css_class('error')

    def segment_range_set(self, microseconds, init=False):
        delta = timedelta(microseconds=microseconds)
        total_seconds = delta.total_seconds()

        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = total_seconds % 60
        seconds_str = f'{seconds:06.3f}'

        if init:
            if hours > 0:
                self.segment_format = 'h'
                self.segment_format_options = data.time_format_options['h']
            elif minutes > 0:
                self.segment_format = 'm'
                self.segment_format_options = data.time_format_options['m']
            else:
                self.segment_format = 's'
                self.segment_format_options = data.time_format_options['s']
            self.w.segment.set_tooltip_text(
                f'{self.segment_format_options[2]}\n\n{data.timestamp_help}'
            )

        match self.segment_format:
            case 'h': value = f'{hours:02d}:{minutes:02d}:{seconds_str}'
            case 'm': value = f'{minutes:02d}:{seconds_str}'
            case 's': value = seconds_str

        match self.segment_point:
            case 0:
                self.segment_value_start = 0
                self.segment_value_end = microseconds
                self.w.segment_entry_start.set_text(
                    self.segment_format_options[0])
                self.w.segment_entry_end.set_text(value)
            case 1:  # start
                if microseconds > self.segment_value_end:
                    return
                self.w.segment_entry_start.remove_css_class('error')
                self.segment_value_start = microseconds
                self.w.segment_entry_start.set_text(value)
            case 2:  # end
                if microseconds < self.segment_value_start:
                    return
                self.w.segment_entry_end.remove_css_class('error')
                self.segment_value_end = microseconds
                self.w.segment_entry_end.set_text(value)

    def segment_range_get(self):
        duration = self.segment_value_end - self.segment_value_start
        return (
            ['-ss', self.microseconds_to_hms(self.segment_value_start)],
            ['-t', self.microseconds_to_hms(duration)],
        )

    def microseconds_to_hms(self, microseconds):
        delta = timedelta(microseconds=microseconds)
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        milliseconds = delta.microseconds // 1000
        return f'{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}'

    def text_to_microseconds(self, text: str):
        text = text.strip()

        parts = text.split(':')
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            split = parts[2].split('.')
            seconds = int(split[0])
            milliseconds = int(split[1].ljust(3, '0')[:3])
        elif len(parts) == 2:
            hours = 0
            minutes = int(parts[0])
            split = parts[1].split('.')
            seconds = int(split[0])
            milliseconds = int(split[1].ljust(3, '0')[:3])
        elif len(parts) == 1:
            hours = 0
            minutes = 0
            split = parts[0].split('.')
            seconds = int(split[0])
            milliseconds = int(split[1].ljust(3, '0')[:3])
        else:
            raise ValueError(
                f'Format parsing error: option "{text}" is invalid')

        return (hours * 3600 + minutes * 60 + seconds) * 1000000 \
            + milliseconds * 1000

    # -------------------------------------------------------------------------

    def preview_switch(self, widget, _):
        if self.result != '':
            if widget.get_active():
                self.disconnect_stream()
                self.w.crop_toggle.set_active(False)
                self.w.crop_toggle.set_sensitive(False)
                self.w.crop_reset.set_sensitive(False)
                if self.file_format in ('.webp', '.avif'):
                    self.stack_adjust_visibility('external')
                else:
                    self.w.video.set_filename(self.result)
                    self.stack_adjust_visibility('display')
                self.current = self.result
                self.update_crop_overlay()
                self.update_frames_controls()
                self.w.preview.add_css_class('success')
                # trim & segment
                self.w.trim.set_sensitive(False)
                self.w.crop_toggle.set_sensitive(False)
                self.w.frames_toggle.set_sensitive(False)
                self.w.frames_remove.set_sensitive(False)
                self.w.frames_clear.set_sensitive(False)
                self.w.segment.set_visible(False)
            else:
                self.stack_adjust_visibility('display')
                self.w.video.set_filename(self.source)
                self.current = self.source
                self.w.crop_toggle.set_sensitive(True)
                self.w.crop_reset.set_sensitive(self.crop_rect is not None)
                self.w.preview.remove_css_class('success')
                # trim, segment & stream
                self.w.trim.set_sensitive(True)
                self.w.frames_toggle.set_sensitive(bool(self.frame_buttons))
                self.update_frames_controls()
                self.w.segment.set_visible(self.enable_trim)
                self.connect_stream()
                self.update_crop_overlay()

    def toast_button_show(self, _, fp: str):
        fd = os.path.dirname(fp)
        if os.path.isdir(fd):
            subprocess.run(['xdg-open', fd])

    def browser_preview(self, _):
        file = Gio.File.new_for_path(self.result)
        launcher = Gtk.FileLauncher(file=file)
        launcher.launch(
            parent=self.w,
            cancellable=None,
            callback=self.browser_preview_finish,
        )

    def browser_preview_finish(self, launcher, result):
        try:
            launcher.launch_finish(result)
        except GLib.Error:
            pass

    # -------------------------------------------------------------------------

    def options_load(self):
        for k in self.settings.keys():
            if k in self.options_exceptions:
                continue
            elif k == 'ratio':
                self.options[k] = self.settings.get_boolean(k)
            else:
                self.options[k] = self.settings.get_int(k)

    def options_save(self):
        self.options_get()
        for k in self.options:
            if k == 'ratio':
                self.settings.set_boolean(k, self.options[k])
            else:
                self.settings.set_int(k, self.options[k])

    def options_set(self):
        self.w.image_size.set_selected(self.options['image-size'])
        self.w.image_width.set_value(self.options['image-width'])
        self.w.image_height.set_value(self.options['image-height'])
        self.w.scaler.set_selected(self.options['scaler'])
        self.w.keep_aspect_ratio.set_active(self.options['ratio'])
        self.w.framerate.set_value(self.options['fps'])
        self.w.dither.set_selected(self.options['dither'])
        self.w.max_colors.set_value(self.options['max-colors'])
        self.w.format.set_selected(self.options['format'])
        self.ratio_state(self.w.keep_aspect_ratio, None)

    def options_get(self):
        self.options = {
            'image-width': int(self.w.image_width.get_value()),
            'image-height': int(self.w.image_height.get_value()),
            'scaler': self.w.scaler.get_selected(),
            'ratio': self.w.keep_aspect_ratio.get_active(),
            'fps': int(self.w.framerate.get_value()),
            'max-colors': self.w.max_colors.get_value(),
            'dither': self.w.dither.get_selected(),
            'format': self.w.format.get_selected(),
        }
        size = self.w.image_size.get_selected()
        if size != 0:  # 0 == original
            self.options['image-size'] = size

    # -------------------------------------------------------------------------

    def preparation(self):
        self.result = os.path.join(self.dir, TMP_NAME + self.file_format)

        width = self.options['image-width']
        height = self.options['image-height']
        fps = self.options['fps']

        if self.options['ratio']:
            scale = f"scale={width}:-2"
        else:
            scale = f'scale={width}:{height}'

        scaler = data.scaler[self.options['scaler']]
        if self.settings.get_boolean('accurate-rnd'):
            scaler += '+accurate_rnd'

        uno = self.crop_filter() + f'fps={fps},'
        exclusions = self.frame_exclusions_for_trim()
        if exclusions:
            ranges = '+'.join(
                f'eq(n\\,{start})' if start == end
                else f'between(n\\,{start}\\,{end})'
                for start, end in exclusions
            )
            uno += f"select='not({ranges})',setpts=N/FRAME_RATE/TB,"
        uno += f'{scale}:flags={scaler}'

        if self.file_format == '.gif':
            # palette generation
            dither = data.dither[self.options['dither']]
            if dither == 'bayer':
                bs = self.settings.get_int('bayer-scale')
                dither += ':bayer_scale=' + str(bs)

            palette = data.palette[self.settings.get_int('stats-mode')]
            palette += f":max_colors={self.options['max-colors']}"

            dos = f"{uno},palettegen=stats_mode={palette}"
            tres = f"paletteuse=dither={dither}:diff_mode=rectangle"
        else:
            dos, tres = None, None  # for palette only

        if self.file_format == '.webp':
            cuatro = [
                '-c:v', 'libwebp',
                '-lossless',
                '1' if self.settings.get_boolean('webp-lossless') else '0',
                '-q:v',
                str(self.settings.get_int('webp-quality')),
                '-preset',
                data.webp_presets[self.settings.get_int('webp-preset')],
                '-compression_level',
                str(self.settings.get_int('webp-compression')),
                '-loop', '0', '-fps_mode', 'passthrough', '-y',
            ]
        elif self.file_format == '.avif':
            cuatro = [
                '-c:v', 'libaom-av1',
                '-crf', str(self.settings.get_int('avif-quality')),
                '-b:v', '0',
                '-cpu-used', str(self.settings.get_int('avif-speed')),
                '-threads', '0',
                '-fps_mode', 'passthrough', '-y',
            ]
        else:
            cuatro = ['-fps_mode', 'passthrough', '-y']

        return (uno, dos, tres, cuatro)

    def generate(self, uno, dos, tres, cuatro):
        try:
            duration = []
            if self.enable_trim:
                seek, duration = self.segment_range_get()
                src = [*seek, '-i', self.source]
            else:
                src = ['-i', self.source,]

            cmd = [
                'ffmpeg',
                '-v', 'error',
                '-threads', '0',
                '-filter_threads', '0',
                *src, '-an',
            ]

            if self.file_format == '.gif':
                # palette
                result = subprocess.run([
                    'ffmpeg', '-v', 'error', *src, *duration,
                    '-vf', dos, '-y', self.palette,
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode != 0:
                    err = result.stderr.decode('utf-8').strip()
                    GLib.idle_add(
                        self.generation_failed, 'Palette error', err)
                    return
                cmd.extend((
                    '-i', self.palette, *duration,
                    '-filter_complex', f'{uno} [x]; [x][1:v] {tres}',
                    *cuatro, self.result,
                ))
            else:
                cmd.extend((*duration, '-vf', uno, *cuatro, self.result))

            # conversion
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode != 0:
                err = result.stderr.decode('utf-8').strip()
                GLib.idle_add(
                    self.generation_failed, 'Generation error', err)
                return
        except Exception as err:
            GLib.idle_add(
                self.generation_failed, 'Generation error', str(err))
            return

        GLib.idle_add(self.generation_complete)

    def generation_failed(self, message: str, detail: str):
        self.generating = False
        self.result = ''
        self.stack_adjust_visibility('display')
        if self.source != '':
            # return to source stream; otherwise trim to avoid stale result
            self.w.video.set_filename(self.source)
            self.current = self.source
            self.connect_stream()
            self.update_crop_overlay()
        self.switch_control(generate=True, preview=False, save=False)
        self.w.trim.set_sensitive(True)
        self.w.crop_toggle.set_sensitive(self.sources_size is not None)
        self.w.crop_reset.set_sensitive(self.crop_rect is not None)
        self.w.frames_toggle.set_sensitive(bool(self.frame_buttons))
        self.update_frames_controls()
        self.w.segment.set_visible(self.enable_trim)
        self.message_show(message, detail)

    def generation_complete(self):
        self.generating = False
        basename = os.path.splitext(os.path.basename(self.source))[0]
        self.name = basename + self.file_format

        file_size = round(os.path.getsize(self.result) / (1024 ** 2), 1)
        file_size = locale.format_string('%.1f', file_size)
        self.w.overlay.add_toast(Adw.Toast(
            title=f'{self.w.ts_size} {file_size}',
            timeout=4,
        ))

        self.w.video.set_filename(self.result)
        self.current = self.result
        self.w.crop_toggle.set_sensitive(False)
        self.w.crop_reset.set_sensitive(False)
        self.w.frames_toggle.set_sensitive(bool(self.frame_buttons))
        self.update_frames_controls()
        self.switch_control(generate=True, preview=True, save=True)

    def generate_wrapper(self, _):
        if self.all_output_frames_excluded():
            self.message_show(
                self.w.ts_error_frames, self.w.ts_error_frames_detail)
            return
        self.generating = True
        self.segment_point = 0
        self.w.segment_box_start.remove_css_class('success')
        self.w.segment_box_end.remove_css_class('success')
        self.switch_control(generate=False, preview=False, save=False)
        self.w.trim.set_sensitive(False)
        self.w.crop_toggle.set_sensitive(False)
        self.w.crop_reset.set_sensitive(False)
        self.w.frames_toggle.set_sensitive(False)
        self.w.frames_remove.set_sensitive(False)
        self.w.frames_clear.set_sensitive(False)
        self.w.segment.set_visible(False)
        self.update_frames_controls()
        self.stack_adjust_visibility('spinner')
        self.options_save()
        args = self.preparation()
        thread = threading.Thread(target=self.generate, args=args, daemon=True)
        thread.start()

    # -------------------------------------------------------------------------

    def about_action(self, *_args):
        about = Adw.AboutDialog(
            application_name='ImageFlow',
            application_icon='tech.digiroad.ImageFlow',
            developer_name='oluka007 (fork maintainer)',
            version=self.version,
            comments=(self.w.ts_comment),
            website='https://github.com/oluka007/ImageFlow',
            developers=[
                'oluka007 (fork maintainer)',
                'Golodnikov Sergey (original creator) '
                '<nn19051990@gmail.com>',
            ],
            artists=[
                'Golodnikov Sergey <nn19051990@gmail.com>',
                'GNOME Design Team https://welcome.gnome.org/team/design',
            ],
            copyright='Copyright © 2026 Golodnikov Sergey',
            license_type=Gtk.License.GPL_3_0,
        )
        about.add_link((self.w.ts_src), 'https://github.com/oluka007/ImageFlow')
        about.present(self.props.active_window)

    def preferences_action(self, _widget, _):
        self.w.pref_theme.set_selected(
            self.settings.get_int('theme'))
        self.w.detect_size.set_active(
            self.settings.get_boolean('detect-size'))
        self.w.accurate_rnd.set_active(
            self.settings.get_boolean('accurate-rnd'))
        self.w.stats_mode.set_selected(
            self.settings.get_int('stats-mode'))
        self.w.bayer_scale.set_value(
            self.settings.get_int('bayer-scale'))
        self.w.webp_lossless.set_active(
            self.settings.get_boolean('webp-lossless'))
        self.w.webp_quality.set_value(
            self.settings.get_int('webp-quality'))
        self.w.webp_preset.set_selected(
            self.settings.get_int('webp-preset'))
        self.w.webp_compression.set_value(
            self.settings.get_int('webp-compression'))
        self.w.avif_quality.set_value(
            self.settings.get_int('avif-quality'))
        self.w.avif_speed.set_value(
            self.settings.get_int('avif-speed'))
        self.w.pref_dialog.present(self.props.active_window)

    def preferences_save(self, _):
        self.settings.set_int(
            'theme', self.w.pref_theme.get_selected())
        self.settings.set_boolean(
            'detect-size', self.w.detect_size.get_active())
        self.settings.set_boolean(
            'accurate-rnd', self.w.accurate_rnd.get_active())
        self.settings.set_int(
            'stats-mode', int(self.w.stats_mode.get_selected()))
        self.settings.set_int(
            'bayer-scale', int(self.w.bayer_scale.get_value()))
        self.settings.set_boolean(
            'webp-lossless', self.w.webp_lossless.get_active())
        self.settings.set_int(
            'webp-quality', int(self.w.webp_quality.get_value()))
        self.settings.set_int(
            'webp-preset', int(self.w.webp_preset.get_selected()))
        self.settings.set_int(
            'webp-compression', int(self.w.webp_compression.get_value()))
        self.settings.set_int(
            'avif-quality', int(self.w.avif_quality.get_value()))
        self.settings.set_int(
            'avif-speed', int(self.w.avif_speed.get_value()))

    def theme_change(self, widget, _):
        self.settings.set_boolean('theme-user', True)
        self.update_theme(widget.get_selected())

    def update_theme(self, value: int):
        style_manager = Adw.StyleManager.get_default()
        if value == 0:
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        else:
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    def get_system_color_scheme(self) -> int:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            'org.freedesktop.portal.Desktop',
            '/org/freedesktop/portal/desktop',
            'org.freedesktop.portal.Settings',
            None,
        )
        try:
            scheme = proxy.Read('(ss)',
                                'org.freedesktop.appearance',
                                'color-scheme')
        except Exception:
            scheme = 2
        # note, scheme: 1 = Dark, 2 = Light
        return 1 if scheme == 1 else 0

    def create_action(self, name, callback, shortcuts=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect('activate', callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f'app.{name}', shortcuts)

    def message_show(self, message: str, detail: str):
        dialog = Gtk.AlertDialog(
            message=message,
            detail=detail,
            buttons=(self.w.ts_cancel,),
        )
        dialog.show(self.w)

    def do_shutdown(self):
        self.frame_generation += 1
        self.cancel_frame_extraction()
        self.frame_temp.cleanup()
        # deleting temporary files
        if self.palette != '' and os.path.exists(self.palette):
            os.remove(self.palette)
        for i in data.format:
            file = os.path.join(self.dir, TMP_NAME + i)
            if os.path.exists(file):
                os.remove(file)
        # shutdown
        Gio.Application.do_shutdown(self)


def main(version):
    app = ImageFlowApplication(version)
    return app.run(sys.argv)
