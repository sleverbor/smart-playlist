"""Structured progress and cooperative cancellation, independent of a UI."""
from datetime import datetime, timezone
from threading import Event
import time
from uuid import uuid4
from .client import SpotifyError


class Cancelled(SpotifyError):
    pass


class Progress:
    def __init__(self, store=None, listener=None, cancellation=None):
        self.store=store
        self.listener=listener
        self.cancellation=cancellation or Event()
        self.operation=uuid4().hex
        self.started=time.monotonic()

    def check(self):
        if self.cancellation.is_set():
            raise Cancelled('Operation cancelled; saved checkpoints or pending writes can be resumed')

    def emit(self,stage,message='',**fields):
        self.check()
        event=dict(operation=self.operation,stage=stage,message=message,
                   timestamp=datetime.now(timezone.utc).isoformat(),elapsed_seconds=time.monotonic()-self.started,**fields)
        if self.store and hasattr(self.store,'event'):
            self.store.event(self.operation,event)
        if self.listener:
            self.listener(event)
        return event

    def failed(self,exc):
        event=dict(operation=self.operation,stage='interrupted' if isinstance(exc,(Cancelled,KeyboardInterrupt)) else 'failed',
                   error_type=type(exc).__name__,message=str(exc),timestamp=datetime.now(timezone.utc).isoformat(),elapsed_seconds=time.monotonic()-self.started)
        if self.store and hasattr(self.store,'event'):
            self.store.event(self.operation,event)
        return event
