"""Transactional SQLite persistence with reversible legacy JSON import."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import time

from .client import read_json, save_json, SpotifyError


class JsonStore:
    def __init__(self, root):
        self.root = Path(root)
    def read(self, path, default=None):
        return read_json(path, default)
    def write(self, path, value):
        save_json(path, value)


class SQLiteStore:
    def __init__(self, root, database=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = Path(database) if database else self.root / 'library.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace = str(self.root)
        self.db = sqlite3.connect(self.path, timeout=30)
        os.chmod(self.path, 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0,1):
            raise SpotifyError('Unsupported database version; preserve database and upgrade the application')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, migrated INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS generated_targets(account TEXT, target TEXT, PRIMARY KEY(account,target));
          CREATE TABLE IF NOT EXISTS documents(workspace TEXT, name TEXT, payload TEXT, updated REAL,
            PRIMARY KEY(workspace,name));
          CREATE TABLE IF NOT EXISTS sequences(workspace TEXT, kind TEXT, position INTEGER, track_id TEXT, payload TEXT,
            PRIMARY KEY(workspace,kind,position));
          CREATE INDEX IF NOT EXISTS sequence_track ON sequences(workspace,track_id);
          CREATE TABLE IF NOT EXISTS imports(workspace TEXT, source TEXT, snapshot TEXT, PRIMARY KEY(workspace,source));
          CREATE TABLE IF NOT EXISTS import_items(workspace TEXT, source TEXT, position INTEGER, track_id TEXT, payload TEXT,
            PRIMARY KEY(workspace,source,position));
          CREATE INDEX IF NOT EXISTS source_track ON import_items(workspace,track_id,source);
          CREATE TABLE IF NOT EXISTS tracks(workspace TEXT, track_id TEXT, album_id TEXT, payload TEXT,
            PRIMARY KEY(workspace,track_id));
          CREATE INDEX IF NOT EXISTS track_album ON tracks(workspace,album_id);
          CREATE TABLE IF NOT EXISTS attributes(workspace TEXT, track_id TEXT, kind TEXT, value TEXT,
            PRIMARY KEY(workspace,track_id,kind,value));
          CREATE INDEX IF NOT EXISTS attribute_lookup ON attributes(workspace,kind,value,track_id);
          CREATE TABLE IF NOT EXISTS cache(scope TEXT, name TEXT, playlist TEXT, payload TEXT, updated REAL,
            PRIMARY KEY(scope,name));
          CREATE INDEX IF NOT EXISTS cache_playlist ON cache(scope,playlist);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, workspace TEXT, operation TEXT,
            created REAL, payload TEXT);
          CREATE INDEX IF NOT EXISTS event_operation ON events(workspace,operation,id);
          PRAGMA user_version=1;
        ''')
        self.db.execute('INSERT OR IGNORE INTO workspaces(id) VALUES(?)',(self.workspace,))
        self.db.commit()
        migrated = self.db.execute('SELECT migrated FROM workspaces WHERE id=?',(self.workspace,)).fetchone()[0]
        if not migrated:
            with self.db:
                for path in self.root.glob('*.json'):
                    if path.name not in ('token.json','rate.json'):
                        self._write(path.name, read_json(path))
                self.db.execute('UPDATE workspaces SET migrated=1 WHERE id=?',(self.workspace,))

    def close(self):
        from .retention import maintain
        try:
            maintain(self)
        finally:
            self.db.close()

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            raise SpotifyError('Nested application transactions are not supported')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def read(self, path, default=None):
        name = Path(path).name
        row = self.db.execute('SELECT payload FROM documents WHERE workspace=? AND name=?',
                              (self.workspace,name)).fetchone()
        if row is None:
            return default
        value = json.loads(row[0])
        if name == 'state.json':
            for kind in ('history','queue'):
                value[kind] = [json.loads(r[0]) for r in self.db.execute(
                    'SELECT payload FROM sequences WHERE workspace=? AND kind=? ORDER BY position',(self.workspace,kind))]
        if name == 'setup.json':
            for source, entry in value.get('imports',{}).items():
                entry['tracks'] = [json.loads(r[0]) for r in self.db.execute(
                    'SELECT payload FROM import_items WHERE workspace=? AND source=? ORDER BY position',(self.workspace,source))]
        return value

    def write(self, path, value):
        if self.db.in_transaction:
            self._write(Path(path).name,value)
        else:
            with self.db:
                self._write(Path(path).name,value)

    def _track(self, raw):
        if not raw or not raw.get('id'):
            return
        identity=raw['id']
        album=raw.get('album'); album=album.get('id') if isinstance(album,dict) else album
        payload=json.dumps(raw,separators=(',',':'))
        existing=self.db.execute('SELECT payload FROM tracks WHERE workspace=? AND track_id=?',(self.workspace,identity)).fetchone()
        if existing and existing[0]==payload:return
        self.db.execute('INSERT INTO tracks VALUES(?,?,?,?) ON CONFLICT(workspace,track_id) DO UPDATE SET album_id=excluded.album_id,payload=excluded.payload',
                        (self.workspace,identity,album,payload))
        self.db.execute('DELETE FROM attributes WHERE workspace=? AND track_id=?',(self.workspace,identity))
        for kind in ('artists','genres'):
            for attribute in raw.get(kind,[]):
                value=attribute.get('id') if isinstance(attribute,dict) else attribute
                if value:
                    self.db.execute('INSERT OR IGNORE INTO attributes VALUES(?,?,?,?)',(self.workspace,identity,kind,value))

    def _write(self, name, value):
        if name == 'setup.json':
            value = {k: deepcopy(v) for k,v in value.items() if k != 'imports'} | {
                'imports': {source: {k:deepcopy(v) for k,v in entry.items() if k != 'tracks'} |
                            {'tracks':entry.get('tracks',[])} for source,entry in value.get('imports',{}).items()}}
        else:
            value = deepcopy(value)
        if name == 'state.json':
            for kind in ('history','queue'):
                entries=value.pop(kind,[])
                previous={position:payload for position,payload in self.db.execute('SELECT position,payload FROM sequences WHERE workspace=? AND kind=?',(self.workspace,kind))}
                changed=[]
                for position,track in enumerate(entries):
                    payload=json.dumps(track,separators=(',',':'))
                    if previous.get(position)==payload:continue
                    self._track(track)
                    changed.append((self.workspace,kind,position,track['id'],payload))
                self.db.executemany('INSERT INTO sequences VALUES(?,?,?,?,?) ON CONFLICT(workspace,kind,position) DO UPDATE SET track_id=excluded.track_id,payload=excluded.payload',changed)
                if len(previous)>len(entries):self.db.execute('DELETE FROM sequences WHERE workspace=? AND kind=? AND position>=?',(self.workspace,kind,len(entries)))
        if name == 'setup.json':
            imports=value.get('imports',{})
            for source,snapshot in list(self.db.execute('SELECT source,snapshot FROM imports WHERE workspace=?',(self.workspace,))):
                if source not in imports or imports[source]['snapshot'] != snapshot:
                    self.db.execute('DELETE FROM import_items WHERE workspace=? AND source=?',(self.workspace,source))
                    self.db.execute('DELETE FROM imports WHERE workspace=? AND source=?',(self.workspace,source))
            for source,entry in imports.items():
                tracks=entry.pop('tracks',[])
                self.db.execute('INSERT OR REPLACE INTO imports VALUES(?,?,?)',(self.workspace,source,entry['snapshot']))
                count=self.db.execute('SELECT count(*) FROM import_items WHERE workspace=? AND source=?',(self.workspace,source)).fetchone()[0]
                if len(tracks)<count:
                    raise SpotifyError('Import checkpoint shrank without a new snapshot')
                for position,track in enumerate(tracks[count:],count):
                    self._track(track)
                    self.db.execute('INSERT INTO import_items VALUES(?,?,?,?,?)',
                        (self.workspace,source,position,(track or {}).get('id'),json.dumps(track,separators=(',',':'))))
        self.db.execute('INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(workspace,name) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                        (self.workspace,name,json.dumps(value,separators=(',',':')),time.time()))

    def cache_read(self,scope,name):
        row=self.db.execute('SELECT payload FROM cache WHERE scope=? AND name=?',(scope,name)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_write(self,scope,name,value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?)',
                            (scope,name,value.get('playlist'),json.dumps(value,separators=(',',':')),time.time()))

    def cache_delete(self,scope,name=None,playlist=None):
        with self.db:
            if name is not None:
                self.db.execute('DELETE FROM cache WHERE scope=? AND name=?',(scope,name))
            else:
                self.db.execute('DELETE FROM cache WHERE scope=? AND playlist=?',(scope,playlist))

    def event(self, operation, payload):
        with self.db:
            self.db.execute('INSERT INTO events(workspace,operation,created,payload) VALUES(?,?,?,?)',
                            (self.workspace,operation,time.time(),json.dumps(payload,separators=(',',':'))))

    def latest_event(self):
        row=self.db.execute('SELECT payload FROM events WHERE workspace=? ORDER BY id DESC LIMIT 1',(self.workspace,)).fetchone()
        return json.loads(row[0]) if row else None

    def export(self, directory):
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        for (name,) in self.db.execute('SELECT name FROM documents WHERE workspace=?',(self.workspace,)):
            save_json(directory/name,self.read(name))
