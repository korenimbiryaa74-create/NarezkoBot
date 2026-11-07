"""Job-scoped console/file diagnostics. Never enable raw HTTP wire logging."""
from __future__ import annotations
import html
from collections import deque
import importlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
import uuid

_logger = logging.getLogger('twloader.jobs')
_logger.addHandler(logging.NullHandler())
_logger.propagate = False
_local = threading.local()
_secrets = set()
_registry = {}
_history = deque(maxlen=50)
_registry_lock = threading.Lock()


def redact(value):
    text = str(value)
    for secret in sorted(_secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    # Token-bearing URL query strings must not appear even in requests tracebacks.
    text = re.sub(r'(https?://[^\s?<>"\']+)\?[^\s<>"\']+', r'\1?[REDACTED]', text)
    text = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+', r'\1[REDACTED]', text)
    text = re.sub(r'\b\d{7,12}:[A-Za-z0-9_-]{25,}', '[REDACTED BOT TOKEN]', text)
    text = re.sub(r'\bgsk_[A-Za-z0-9]{20,}', '[REDACTED GROQ KEY]', text)
    return text


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class BriefFormatter(SafeFormatter):
    def format(self, record):
        clone=logging.makeLogRecord(record.__dict__.copy())
        if clone.exc_info:
            clone.msg=str(clone.getMessage())+": "+str(clone.exc_info[1])
            clone.args=()
        clone.exc_info=None;clone.exc_text=None
        return super().format(clone)


def _collect_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if isinstance(item, str) and item and any(word in lowered for word in ('secret','token','password','api_key','username')):
                _secrets.add(item)
            _collect_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _collect_secrets(item)


def configure(base_dir, config=None, stream=None, hooks=True):
    _secrets.clear()
    _collect_secrets(config or {})
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
        handler.close()
    verbose=bool((config or {}).get("diagnostics",{}).get("verbose",False))
    _logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = SafeFormatter('%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')
    console = logging.StreamHandler(stream or sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter if verbose else BriefFormatter("%(asctime)s %(levelname)s %(message)s",datefmt="%H:%M:%S"))
    _logger.addHandler(console)
    folder = Path(base_dir) / 'logs'
    logfile = folder / 'bot.log'
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(logfile, maxBytes=1_000_000, backupCount=1, encoding='utf-8')
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        handler.setFormatter(formatter)
        _logger.addHandler(handler)
        try:logfile.chmod(0o600)
        except OSError:pass
    except OSError as exc:
        _logger.warning('[SETUP] Файл лога недоступен: %s; консоль продолжает работать', exc)
    if hooks:
        def thread_error(args):
            _logger.error('[THREAD] Необработанная ошибка потока %s', args.thread.name,
                          exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        def main_error(kind, value, tb):
            if kind is KeyboardInterrupt:
                return sys.__excepthook__(kind, value, tb)
            _logger.error('[MAIN] Необработанная ошибка', exc_info=(kind,value,tb))
        threading.excepthook = thread_error
        sys.excepthook = main_error
    event('Бот готов. Лог ошибок и этапов: ' + str(logfile),logging.INFO)
    return logfile


def current():
    return getattr(_local, 'job', None)


def event(message, level=logging.DEBUG):
    job = current()
    _logger.log(level, '[%s] %s', job.id if job else 'SYS', message)


def stage(message, notify=True):
    job = current()
    if job:
        job.stage(message, notify=notify)
    else:
        event(message)


def fail(message):
    job = current()
    if job:
        job.failed = True
    _logger.error('[%s] %s', job.id if job else 'SYS', message, exc_info=sys.exc_info()[0] is not None)


class JobTrace:
    def __init__(self, vod_id, notify=None, heartbeat=20, owner=None):
        self.id = 'RX-' + str(vod_id) + '-' + uuid.uuid4().hex[:6]
        self.owner = owner
        self.notify = notify
        self.heartbeat = max(.01, heartbeat)
        self.started = self.stage_started = time.monotonic()
        self.label = 'Запуск задания'
        self.failed = False
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.parent = None
        self.thread = None

    def __enter__(self):
        self.parent = current()
        _local.job = self
        with _registry_lock:
            _registry[self.id] = self
        event('СТАРТ задания',logging.INFO)
        self.thread = threading.Thread(target=self._heartbeat, name='status-' + self.id, daemon=True)
        self.thread.start()
        return self

    def stage(self, message, notify=True):
        with self.lock:
            changed=message!=self.label
            if changed:
                self.stage_started = time.monotonic()
            self.label = message
        if changed:event('ЭТАП: ' + message,logging.INFO)
        if self.notify and notify:
            try:
                self.notify('⏳ <b>Ищу реакции из стрима</b>\n<code>' + html.escape(self.id) + '</code>\n' + html.escape(redact(message)))
            except Exception as exc:
                if not getattr(self,'_notify_warned',False):
                    self._notify_warned=True
                    event('Telegram не обновляет статус; работа продолжается',logging.WARNING)

    def _heartbeat(self):
        while not self.stop.wait(self.heartbeat):
            with self.lock:
                label, then = self.label, self.stage_started
            now = time.monotonic()
            _logger.debug('[%s] ОЖИДАНИЕ: %s | этап %.0f с; всего %.0f с (это не процент выполнения)',
                         self.id, label, now - then, now - self.started)

    def __exit__(self, kind, value, tb):
        if kind is not None:
            self.failed = True
            _logger.error('[%s] Ошибка на этапе %s', self.id,self.label,exc_info=(kind,value,tb))
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=.2)
        event(('ЗАВЕРШЕНО С ОШИБКОЙ' if self.failed else 'ЗАВЕРШЕНО') + f' за {time.monotonic()-self.started:.1f} с',logging.INFO)
        now = time.monotonic()
        with _registry_lock:
            _registry.pop(self.id, None)
            _history.append({'id':self.id,'owner':self.owner,'label':self.label,
                             'elapsed':now-self.started,'stage_elapsed':now-self.stage_started,
                             'failed':self.failed,'ended':now,'active':False})
        _local.job = self.parent
        return False


def snapshot(owner):
    """Only the requesting chat's active jobs and last completed job; no config."""
    now = time.monotonic()
    rows = []
    with _registry_lock:
        for job in _registry.values():
            if job.owner != owner:
                continue
            with job.lock:
                rows.append({'id':job.id,'label':redact(job.label),'elapsed':now-job.started,
                             'stage_elapsed':now-job.stage_started,'failed':job.failed,'active':True})
        recent = next((r for r in reversed(_history) if r['owner']==owner and now-r['ended']<86400), None)
        if recent:
            rows.append(dict(recent,label=redact(recent['label'])))
    return rows


def ensure_ffmpeg():
    if shutil.which('ffmpeg') and shutil.which('ffprobe'):
        event('FFmpeg готов: ffmpeg и ffprobe найдены')
        return True
    try:
        static_ffmpeg = importlib.import_module('static_ffmpeg')
    except ImportError:
        event('Нет ffmpeg/ffprobe. Установи FFmpeg в PATH или пакет static-ffmpeg.', logging.WARNING)
        return False
    try:
        with JobTrace('FFMPEG'):
            stage('static-ffmpeg: проверка/первая загрузка бинарников', notify=False)
            static_ffmpeg.add_paths()
        ready = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
        if not ready:
            raise RuntimeError('После static-ffmpeg отсутствуют ffmpeg или ffprobe')
        event('FFmpeg готов через static-ffmpeg')
        return True
    except Exception:
        fail('Не удалось подготовить FFmpeg; проверь загрузку static-ffmpeg')
        return False
