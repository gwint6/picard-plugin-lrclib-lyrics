from __future__ import annotations

import json
import os
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from picard.plugin3.api import Album, BaseAction, File, Metadata, OptionsPage, PluginApi, Track
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


PLUGIN_NAME = "LRCLIB Lyrics"
PLUGIN_VERSION = "2.0.0"

LRCLIB_GET_URL = "https://lrclib.net/api/get"
LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
LRCLIB_REQUEST_GAP_MS = 400
LRCLIB_MAX_RETRIES = 5
LRCLIB_PROGRESS_INTERVAL = 25
LRCLIB_CLIENT = (
    f"{PLUGIN_NAME} Picard Plugin/{PLUGIN_VERSION} "
    "(https://github.com/izaz4141/picard-lrclib)"
)

PLUGIN_OPTIONS = {
    "get_on_load": False,
    "get_on_save": False,
    "auto_overwrite": False,
    "save_lrc_file": True,
    "ignore_instrumental": False,
    "plain_as_txt": False,
}

_api: PluginApi | None = None
_files_processing: set[str] = set()
_album_batches: dict[int, dict] = {}
_request_queue: list[dict] = []
_active_request: dict | None = None
_network_manager: QNetworkAccessManager | None = None
_queue_stats = {
    "generation": 0,
    "total": 0,
    "processed": 0,
    "found": 0,
    "not_found": 0,
    "failed": 0,
    "retries": 0,
}


def _plugin_api() -> PluginApi:
    if _api is None:
        raise RuntimeError(f"{PLUGIN_NAME} is not enabled")
    return _api


def _logger():
    return _plugin_api().logger


def _setting(name: str):
    return _plugin_api().plugin_config.get(name, PLUGIN_OPTIONS[name])


def _network() -> QNetworkAccessManager:
    global _network_manager
    if _network_manager is None:
        _network_manager = QNetworkAccessManager(QtWidgets.QApplication.instance())
    return _network_manager


def _start_album_request(album: Album, metadata: Metadata) -> None:
    album_id = id(album)
    batch = _album_batches.setdefault(
        album_id,
        {
            "album": metadata.get("album") or "Unknown album",
            "pending": 0,
            "processed": 0,
            "found": 0,
            "not_found": 0,
            "failed": 0,
            "retries": 0,
            "generation": 0,
        },
    )
    batch["pending"] += 1
    batch["generation"] += 1


def _finish_album_request(album: Album, outcome: str) -> None:
    album_id = id(album)
    batch = _album_batches.get(album_id)
    if batch is None:
        return

    batch["pending"] = max(0, batch["pending"] - 1)
    batch["processed"] += 1
    batch[outcome] += 1
    if batch["pending"]:
        return

    generation = batch["generation"]

    def log_if_still_complete() -> None:
        current = _album_batches.get(album_id)
        if (
            current is None
            or current["pending"]
            or current["generation"] != generation
        ):
            return
        _logger().info(
            "%s: ALBUM COMPLETE — %s — %d processed, %d found, "
            "%d not found, %d failed, %d retries",
            PLUGIN_NAME,
            current["album"],
            current["processed"],
            current["found"],
            current["not_found"],
            current["failed"],
            current["retries"],
        )
        del _album_batches[album_id]

    QtCore.QTimer.singleShot(1000, log_if_still_complete)


def _record_retry(album: Album, is_save_request: bool) -> None:
    _queue_stats["retries"] += 1
    if is_save_request:
        batch = _album_batches.get(id(album))
        if batch is not None:
            batch["retries"] += 1


def _queue_result(outcome: str) -> None:
    _queue_stats["processed"] += 1
    _queue_stats[outcome] += 1
    processed = _queue_stats["processed"]
    if processed % LRCLIB_PROGRESS_INTERVAL == 0:
        _logger().info(
            "%s: QUEUE PROGRESS — %d/%d processed, %d waiting, %d retries",
            PLUGIN_NAME,
            processed,
            _queue_stats["total"],
            len(_request_queue),
            _queue_stats["retries"],
        )


def _schedule_queue_complete() -> None:
    generation = _queue_stats["generation"]

    def log_if_still_idle() -> None:
        if (
            _active_request is not None
            or _request_queue
            or _queue_stats["generation"] != generation
        ):
            return
        _logger().info(
            "%s: QUEUE COMPLETE — %d processed, %d found, %d not found, "
            "%d failed, %d retries",
            PLUGIN_NAME,
            _queue_stats["processed"],
            _queue_stats["found"],
            _queue_stats["not_found"],
            _queue_stats["failed"],
            _queue_stats["retries"],
        )

    QtCore.QTimer.singleShot(1500, log_if_still_idle)


def _enqueue_request(
    method: str,
    album: Album,
    metadata: Metadata,
    linked_files: list[File],
    queryargs: dict,
) -> None:
    was_idle = _active_request is None and not _request_queue
    if was_idle:
        _queue_stats.update(
            total=0,
            processed=0,
            found=0,
            not_found=0,
            failed=0,
            retries=0,
        )

    _queue_stats["generation"] += 1
    _queue_stats["total"] += 1
    _request_queue.append(
        {
            "method": method,
            "album": album,
            "metadata": metadata,
            "linked_files": linked_files,
            "queryargs": queryargs,
            "attempt": 1,
        }
    )
    _logger().debug(
        '%s: queued "%s" (queue position %d)',
        PLUGIN_NAME,
        metadata.get("title") or "Unknown track",
        len(_request_queue),
    )
    if was_idle:
        _logger().info(
            "%s: API queue active — sequential requests with a %d ms gap",
            PLUGIN_NAME,
            LRCLIB_REQUEST_GAP_MS,
        )
        QtCore.QTimer.singleShot(0, _start_next_request)


def _start_next_request() -> None:
    global _active_request
    if _active_request is not None or not _request_queue:
        if _active_request is None and not _request_queue:
            _schedule_queue_complete()
        return

    item = _request_queue.pop(0)
    _active_request = item
    request = QNetworkRequest(
        QtCore.QUrl(f"{LRCLIB_GET_URL}?{urlencode(item['queryargs'])}")
    )
    client_header = LRCLIB_CLIENT.encode("utf-8")
    request.setRawHeader(b"User-Agent", client_header)
    request.setRawHeader(b"X-User-Agent", client_header)
    request.setRawHeader(b"Lrclib-Client", client_header)
    request.setAttribute(
        QNetworkRequest.Attribute.CacheLoadControlAttribute,
        QNetworkRequest.CacheLoadControl.AlwaysNetwork,
    )
    request.setTransferTimeout(30000)

    _logger().debug(
        '%s: request attempt %d for "%s"',
        PLUGIN_NAME,
        item["attempt"],
        item["metadata"].get("title") or "Unknown track",
    )
    reply = _network().get(request)
    reply.finished.connect(lambda reply=reply: _handle_reply(reply))


def _handle_reply(reply: QNetworkReply) -> None:
    global _active_request
    item = _active_request
    if item is None:
        reply.deleteLater()
        return

    status_value = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    status = int(status_value) if status_value is not None else 0
    network_error = reply.error()
    error_text = reply.errorString()
    body = bytes(reply.readAll()).decode("utf-8", errors="replace")
    retry_after = bytes(reply.rawHeader(b"Retry-After")).decode(
        "ascii", errors="ignore"
    ).strip()
    reply.deleteLater()

    transient = status in {429, 500, 502, 503, 504} or (
        status == 0 and network_error != QNetworkReply.NetworkError.NoError
    )
    if transient and item["attempt"] <= LRCLIB_MAX_RETRIES:
        if status == 429 and retry_after.isdigit():
            delay_ms = max(1000, int(retry_after) * 1000)
        else:
            delay_ms = min(16000, 1000 * (2 ** (item["attempt"] - 1)))

        item["attempt"] += 1
        _record_retry(item["album"], item["method"] == "get_on_save")
        _logger().warning(
            '%s: API retry — "%s" — %s — attempt %d/%d in %.1f s',
            PLUGIN_NAME,
            item["metadata"].get("title") or "Unknown track",
            f"HTTP {status}" if status else error_text,
            item["attempt"],
            LRCLIB_MAX_RETRIES + 1,
            delay_ms / 1000,
        )
        _active_request = None
        _request_queue.insert(0, item)
        QtCore.QTimer.singleShot(delay_ms, _start_next_request)
        return

    _active_request = None
    if status == 200 and network_error == QNetworkReply.NetworkError.NoError:
        try:
            response = json.loads(body)
            outcome = _process_response(item, response, None)
        except (TypeError, ValueError) as exc:
            outcome = _process_response(item, None, f"invalid JSON response: {exc}")
    elif status == 404:
        outcome = _process_response(item, {"code": 404}, None)
    else:
        detail = f"HTTP {status}" if status else error_text
        outcome = _process_response(item, None, detail)

    _queue_result(outcome)
    QtCore.QTimer.singleShot(LRCLIB_REQUEST_GAP_MS, _start_next_request)


def format_duration(duration: int) -> str:
    hours, remainder = divmod(int(duration), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def parse_duration(time_str: str) -> int:
    parts = time_str.strip().split(":")
    if not all(part.isdigit() for part in parts):
        raise ValueError(f"Invalid time format: {time_str}")
    if len(parts) == 2:
        minutes, seconds = map(int, parts)
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = map(int, parts)
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Unsupported time format: {time_str}")


def get_track_duration(track: Track) -> int:
    value = track.metadata.get("~length")
    if value:
        return parse_duration(str(value))
    if track.files and track.files[0].metadata.get("~length"):
        return parse_duration(str(track.files[0].metadata["~length"]))
    raise ValueError(f"No duration found for {track.metadata.get('title', 'track')}")


def truncate_text(text: str, max_lines: int = 5, max_chars: int = 46) -> str:
    source_lines = text.splitlines()
    lines = []
    for line in source_lines[:max_lines]:
        if len(line) > max_chars:
            line = line[: max_chars - 1].rstrip() + "…"
        lines.append(line)
    if len(source_lines) > max_lines and lines:
        lines[-1] = lines[-1].rstrip() + " …"
    return "\n".join(lines)


def confirm_replace(parent, title: str, description: str) -> bool:
    parent = QtWidgets.QApplication.activeWindow() if parent is None else parent
    buttons = (
        QtWidgets.QMessageBox.StandardButton.Yes
        | QtWidgets.QMessageBox.StandardButton.No
    )
    reply = QtWidgets.QMessageBox.question(
        parent,
        title,
        description,
        buttons,
        QtWidgets.QMessageBox.StandardButton.No,
    )
    return reply == QtWidgets.QMessageBox.StandardButton.Yes


def _fetch_json(url: str, params: dict):
    try:
        full_url = f"{url}?{urlencode(params)}"
        request = Request(
            full_url,
            headers={
                "User-Agent": LRCLIB_CLIENT,
                "X-User-Agent": LRCLIB_CLIENT,
                "Lrclib-Client": LRCLIB_CLIENT,
            },
        )
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        _logger().error("%s: search request failed: %s", PLUGIN_NAME, exc)
        return []


def show_search_table(parent, query: str, response, request_callback):
    parent = QtWidgets.QApplication.activeWindow() if parent is None else parent
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Search Tracks")
    dialog.resize(700, 400)
    layout = QtWidgets.QVBoxLayout(dialog)

    search_layout = QtWidgets.QHBoxLayout()
    search_input = QtWidgets.QLineEdit(query)
    search_input.setPlaceholderText("Enter search query...")
    search_button = QtWidgets.QPushButton("Search")
    search_button.setDefault(True)
    search_layout.addWidget(search_input)
    search_layout.addWidget(search_button)
    layout.addLayout(search_layout)

    table = QtWidgets.QTableWidget(dialog)
    table.setColumnCount(6)
    table.setHorizontalHeaderLabels(
        ["#", "Name", "Artist", "Length", "Album", "Synced"]
    )
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    header.setDefaultAlignment(
        QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignVCenter
    )
    modes = [
        QtWidgets.QHeaderView.ResizeMode.ResizeToContents,
        QtWidgets.QHeaderView.ResizeMode.Stretch,
        QtWidgets.QHeaderView.ResizeMode.Interactive,
        QtWidgets.QHeaderView.ResizeMode.ResizeToContents,
        QtWidgets.QHeaderView.ResizeMode.Interactive,
        QtWidgets.QHeaderView.ResizeMode.ResizeToContents,
    ]
    for column, mode in enumerate(modes):
        header.setSectionResizeMode(column, mode)
    table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(
        QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
    )
    table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
    layout.addWidget(table)

    button_box = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.StandardButton.Ok
        | QtWidgets.QDialogButtonBox.StandardButton.Cancel
    )
    layout.addWidget(button_box)

    def populate(items) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(len(items or []))
        for row, item in enumerate(items or []):
            number = QtWidgets.QTableWidgetItem()
            number.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            number.setData(QtCore.Qt.ItemDataRole.EditRole, row + 1)
            table.setItem(row, 0, number)
            has_synced = bool(item.get("syncedLyrics"))
            values = [
                item.get("trackName") or "?",
                item.get("artistName") or "?",
                format_duration(item.get("duration") or 0),
                item.get("albumName") or "?",
                "V" if has_synced else "X",
            ]
            for column, value in enumerate(values, 1):
                cell = QtWidgets.QTableWidgetItem(str(value))
                if column in (3, 5):
                    cell.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                if column == 5:
                    cell.setForeground(
                        QtGui.QColor("#2ecc71" if has_synced else "#e74c3c")
                    )
                table.setItem(row, column, cell)
        table.setSortingEnabled(True)

    current_response = response
    populate(current_response)

    def search_again() -> None:
        nonlocal current_response
        new_query = search_input.text().strip()
        if new_query:
            current_response = request_callback(LRCLIB_SEARCH_URL, {"q": new_query})
            populate(current_response)

    search_button.clicked.connect(search_again)
    search_input.returnPressed.connect(search_again)
    table.doubleClicked.connect(lambda index: dialog.accept() if index.isValid() else None)
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)

    if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        selected = table.currentRow()
        return current_response[selected] if selected >= 0 else None
    return None


def fetch_lyrics(
    method: str,
    album: Album,
    metadata: Metadata,
    linked_files: list[File],
    length: int | None = None,
) -> None:
    title = metadata.get("title") or ""
    if method == "search":
        response = _fetch_json(LRCLIB_SEARCH_URL, {"q": title})
        selected = show_search_table(
            QtWidgets.QApplication.activeWindow(), title, response, _fetch_json
        )
        if selected is not None:
            _apply_lyrics(method, metadata, linked_files, selected)
        return

    queryargs = {
        "track_name": title,
        "artist_name": metadata.get("artist") or "",
        "album_name": metadata.get("album") or "",
    }
    if length:
        queryargs["duration"] = length
    _logger().debug(
        "%s: GET %s?%s", PLUGIN_NAME, quote(LRCLIB_GET_URL), urlencode(queryargs)
    )
    _enqueue_request(method, album, metadata, linked_files, queryargs)


def _process_response(item: dict, response, error: str | None) -> str:
    method = item["method"]
    album = item["album"]
    metadata = item["metadata"]
    linked_files = item["linked_files"]

    if error:
        _logger().error(
            '%s: request FAILED for track "%s" by %s: %s',
            PLUGIN_NAME,
            metadata.get("title") or "Unknown track",
            metadata.get("artist") or "Unknown artist",
            error,
        )
        if method == "get_on_save":
            for file in linked_files:
                _files_processing.discard(file.filename)
            _finish_album_request(album, "failed")
        return "failed"

    if not isinstance(response, dict) or not response.get("id"):
        _logger().warning(
            '%s: lyrics NOT found for track "%s" by %s',
            PLUGIN_NAME,
            metadata.get("title") or "Unknown track",
            metadata.get("artist") or "Unknown artist",
        )
        if method == "get_on_save":
            for file in linked_files:
                _files_processing.discard(file.filename)
            _finish_album_request(album, "not_found")
        return "not_found"

    outcome = _apply_lyrics(method, metadata, linked_files, response)
    if method == "get_on_save":
        for file in linked_files:
            if outcome == "found":
                try:
                    file.save()
                except Exception as exc:
                    outcome = "failed"
                    _files_processing.discard(file.filename)
                    _logger().error(
                        "%s: failed to save tags after loading lyrics: %s",
                        PLUGIN_NAME,
                        exc,
                    )
            else:
                _files_processing.discard(file.filename)
        _finish_album_request(album, outcome)
    return outcome


def _apply_lyrics(
    method: str, metadata: Metadata, linked_files: list[File], response: dict
) -> str:
    instrumental = (
        response.get("instrumental", False)
        or "(Instrumental)" in (response.get("trackName") or "")
        or "[au: instrumental]" in (response.get("plainLyrics") or "")
    )
    if instrumental and _setting("ignore_instrumental") and method != "search":
        return "not_found"

    lyrics = response.get("syncedLyrics")
    is_plain = not bool(lyrics)
    if not lyrics:
        lyrics = response.get("plainLyrics")
    if not isinstance(lyrics, str) or not lyrics.strip():
        return "not_found"

    try:
        for file in linked_files:
            extension = ".txt" if is_plain and _setting("plain_as_txt") else ".lrc"
            full_path = file.filename
            if not full_path:
                raise ValueError("File path is empty")
            base_path = os.path.splitext(full_path)[0]
            lyrics_path = base_path + extension

            has_tag_lyrics = bool(file.metadata.get("lyrics"))
            has_sidecar = os.path.exists(lyrics_path)
            selected_lyrics = lyrics
            if (
                has_tag_lyrics
                and not has_sidecar
                and _setting("save_lrc_file")
                and method != "search"
            ):
                selected_lyrics = file.metadata.get("lyrics")
            elif has_sidecar and not has_tag_lyrics and method != "search":
                with open(lyrics_path, "r", encoding="utf-8") as handle:
                    selected_lyrics = handle.read()
            elif (
                has_tag_lyrics
                and (has_sidecar or not _setting("save_lrc_file"))
                and not _setting("auto_overwrite")
                and method not in {"get_on_load", "get_on_save"}
            ):
                description = 'Overwrite Lyrics for "{}"?\n\n{}'.format(
                    file.metadata.get("title", "<file>"),
                    truncate_text(selected_lyrics, 5, 42),
                )
                if not confirm_replace(None, "Overwrite file lyrics?", description):
                    continue

            file.metadata["lyrics"] = selected_lyrics
            if _setting("save_lrc_file"):
                for old_extension in (".txt", ".lrc"):
                    old_path = base_path + old_extension
                    if os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except OSError as exc:
                            _logger().error(
                                "%s: failed to delete %s: %s",
                                PLUGIN_NAME,
                                old_path,
                                exc,
                            )
                with open(lyrics_path, "w", encoding="utf-8") as handle:
                    handle.write(selected_lyrics)

        _logger().debug(
            '%s: lyrics loaded for track "%s" by %s',
            PLUGIN_NAME,
            metadata.get("title") or "Unknown track",
            metadata.get("artist") or "Unknown artist",
        )
        return "found"
    except (OSError, AttributeError, TypeError, KeyError, ValueError) as exc:
        _logger().error(
            '%s: lyrics NOT loaded for track "%s" by %s: %s',
            PLUGIN_NAME,
            metadata.get("title") or "Unknown track",
            metadata.get("artist") or "Unknown artist",
            exc,
            exc_info=True,
        )
        return "failed"


class LrclibLyricsOptionsPage(OptionsPage):
    NAME = "lrclib_lyrics"
    TITLE = "LRCLIB Lyrics"
    PARENT = "plugins"

    AUDIO_EXTENSIONS = {
        "aac", "ac3", "aif", "aifc", "aiff", "ape", "asf", "dff", "dsf",
        "eac3", "flac", "kar", "m2a", "ofr", "ofs", "oga", "ogg", "oggflac",
        "oggtheora", "ogv", "ogx", "opus", "spx", "tak", "tta", "wav", "webm",
        "wma", "wmv", "wv", "xwma",
    }

    def __init__(self, api=None, parent=None):
        super().__init__(parent)
        if api is not None:
            self.api = api
        layout = QtWidgets.QVBoxLayout(self)
        self.get_on_load = QtWidgets.QCheckBox("Search for lyrics when loading tracks")
        self.get_on_save = QtWidgets.QCheckBox("Search for lyrics when saving files")
        self.auto_overwrite = QtWidgets.QCheckBox("Auto overwrite existing lyrics")
        self.save_lrc = QtWidgets.QCheckBox("Save .lrc file alongside audio files")
        self.ignore_instrumental = QtWidgets.QCheckBox("Ignore instrumental lyrics")
        self.plain_as_txt = QtWidgets.QCheckBox("Save plain lyrics as .txt")
        for widget in (
            self.get_on_load,
            self.get_on_save,
            self.auto_overwrite,
            self.save_lrc,
            self.ignore_instrumental,
            self.plain_as_txt,
        ):
            layout.addWidget(widget)

        layout.addSpacing(20)
        cleanup_label = QtWidgets.QLabel("Cleanup Tools:")
        cleanup_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(cleanup_label)
        cleanup_button = QtWidgets.QPushButton("Clean Orphaned LRC Files")
        cleanup_button.setToolTip(
            "Recursively scan a directory for .lrc files without matching audio files"
        )
        cleanup_button.clicked.connect(self.clean_orphaned_lrc_files)
        layout.addWidget(cleanup_button)
        layout.addStretch()
        description = QtWidgets.QLabel(
            "LRCLIB provides lyrics for personal and educational use.\n"
            "Loading-time searches can significantly slow album loading."
        )
        layout.addWidget(description)

    def load(self):
        config = self.api.plugin_config
        self.get_on_load.setChecked(bool(config.get("get_on_load", False)))
        self.get_on_save.setChecked(bool(config.get("get_on_save", False)))
        self.auto_overwrite.setChecked(bool(config.get("auto_overwrite", False)))
        self.save_lrc.setChecked(bool(config.get("save_lrc_file", True)))
        self.ignore_instrumental.setChecked(
            bool(config.get("ignore_instrumental", False))
        )
        self.plain_as_txt.setChecked(bool(config.get("plain_as_txt", False)))

    def save(self):
        config = self.api.plugin_config
        config["get_on_load"] = self.get_on_load.isChecked()
        config["get_on_save"] = self.get_on_save.isChecked()
        config["auto_overwrite"] = self.auto_overwrite.isChecked()
        config["save_lrc_file"] = self.save_lrc.isChecked()
        config["ignore_instrumental"] = self.ignore_instrumental.isChecked()
        config["plain_as_txt"] = self.plain_as_txt.isChecked()

    def clean_orphaned_lrc_files(self):
        options = (
            QtWidgets.QFileDialog.Option.ShowDirsOnly
            | QtWidgets.QFileDialog.Option.DontResolveSymlinks
        )
        root_dir = QtWidgets.QFileDialog.getExistingDirectory(
            QtWidgets.QApplication.activeWindow(),
            "Select Music Library Root Directory",
            "",
            options,
        )
        if not root_dir:
            return
        count = self._clean_directory_recursive(root_dir)
        QtWidgets.QMessageBox.information(
            QtWidgets.QApplication.activeWindow(),
            "Cleanup Complete",
            f"Removed {count} orphaned .lrc file{'s' if count != 1 else ''}",
        )
        self.api.logger.info(
            "%s: cleaned %d orphaned .lrc files", PLUGIN_NAME, count
        )

    def _clean_directory_recursive(self, root_dir: str) -> int:
        count = 0
        for directory, _subdirs, filenames in os.walk(root_dir):
            lower_names = {name.lower() for name in filenames}
            for filename in filenames:
                if not filename.lower().endswith(".lrc"):
                    continue
                stem = os.path.splitext(filename)[0]
                has_audio = any(
                    f"{stem}.{extension}".lower() in lower_names
                    for extension in self.AUDIO_EXTENSIONS
                )
                if not has_audio:
                    path = os.path.join(directory, filename)
                    try:
                        os.remove(path)
                        count += 1
                    except OSError as exc:
                        self.api.logger.error(
                            "%s: failed to delete %s: %s", PLUGIN_NAME, path, exc
                        )
        return count


def get_on_load(api: PluginApi, track: Track, file: File) -> None:
    if not api.plugin_config.get("get_on_load", False) or not track.files:
        return
    try:
        fetch_lyrics(
            "get_on_load",
            track.album,
            track.metadata,
            track.files,
            get_track_duration(track),
        )
    except Exception as exc:
        api.logger.error("%s: error in load hook: %s", PLUGIN_NAME, exc)


def get_on_save(api: PluginApi, file: File) -> None:
    if not api.plugin_config.get("get_on_save", False):
        return
    if file.filename in _files_processing:
        _files_processing.discard(file.filename)
        return

    request_started = False
    album = None
    try:
        _files_processing.add(file.filename)
        album = file.parent.album
        metadata = file.metadata
        length_value = metadata.get("~length")
        if not length_value:
            raise ValueError("Track duration is missing")
        length = parse_duration(str(length_value))
        _start_album_request(album, metadata)
        request_started = True
        fetch_lyrics("get_on_save", album, metadata, [file], length)
    except Exception as exc:
        api.logger.error("%s: error in save hook: %s", PLUGIN_NAME, exc)
        _files_processing.discard(file.filename)
        if request_started and album is not None:
            _finish_album_request(album, "failed")


class LrcLibLyricsGet(BaseAction):
    TITLE = "Get lyrics automatically with LRCLIB"

    def execute_on_track(self, track: Track) -> None:
        if not track.linked_files:
            return
        try:
            fetch_lyrics(
                "get", track.album, track.metadata, track.files, get_track_duration(track)
            )
        except Exception as exc:
            self.api.logger.error("%s: manual lookup failed: %s", PLUGIN_NAME, exc)

    def callback(self, objects):
        for item in objects:
            if isinstance(item, Track):
                self.execute_on_track(item)
            elif isinstance(item, Album):
                for track in item.tracks:
                    self.execute_on_track(track)


class LrcLibLyricsSearch(BaseAction):
    TITLE = "Search lyrics manually with LRCLIB"

    def execute_on_track(self, track: Track) -> None:
        if not track.linked_files:
            return
        try:
            fetch_lyrics("search", track.album, track.metadata, track.linked_files)
        except Exception as exc:
            self.api.logger.error("%s: manual search failed: %s", PLUGIN_NAME, exc)

    def callback(self, objects):
        for item in objects:
            if isinstance(item, Track):
                self.execute_on_track(item)
            elif isinstance(item, Album):
                for track in item.tracks:
                    self.execute_on_track(track)


def _migrate_v2_settings(api: PluginApi) -> None:
    migration_key = "_v2_settings_migrated"
    api.plugin_config.register_option(migration_key, False)
    if api.plugin_config.get(migration_key, False):
        return
    try:
        for name, default in PLUGIN_OPTIONS.items():
            old_value = api.global_config.setting.get(name, None)
            if old_value is not None:
                api.plugin_config[name] = old_value
            elif name not in api.plugin_config:
                api.plugin_config[name] = default
    finally:
        api.plugin_config[migration_key] = True


def enable(api: PluginApi) -> None:
    global _api
    _api = api
    for name, default in PLUGIN_OPTIONS.items():
        api.plugin_config.register_option(name, default)
    _migrate_v2_settings(api)
    api.register_file_post_addition_to_track_processor(get_on_load)
    api.register_file_post_save_processor(get_on_save)
    api.register_track_action(LrcLibLyricsSearch)
    api.register_album_action(LrcLibLyricsSearch)
    api.register_track_action(LrcLibLyricsGet)
    api.register_album_action(LrcLibLyricsGet)
    api.register_options_page(LrclibLyricsOptionsPage)
    api.logger.info("%s %s loaded for Picard 3", PLUGIN_NAME, PLUGIN_VERSION)


def disable() -> None:
    global _api, _active_request, _network_manager
    if _active_request is not None:
        for file in _active_request.get("linked_files", []):
            _files_processing.discard(file.filename)
    for item in _request_queue:
        for file in item.get("linked_files", []):
            _files_processing.discard(file.filename)
    _request_queue.clear()
    _active_request = None
    _album_batches.clear()
    _network_manager = None
    _api = None
