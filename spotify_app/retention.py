"""Bound auxiliary storage without deleting active sync/recovery state."""
import json
import shutil
import time
from pathlib import Path

DAY=86400

def maintain(store, now=None):
    now=time.time() if now is None else now
    db=store.db;workspace=store.workspace
    marker=db.execute("SELECT updated FROM documents WHERE workspace=? AND name='retention.json'",(workspace,)).fetchone()
    if marker and now-marker[0]<DAY:return
    state=store.read('state.json') or {}
    # Pending journals and saved setup remain intact. History backups are pruned
    # only when no remote write is awaiting recovery.
    with db:
        db.execute('DELETE FROM events WHERE workspace=? AND (created<? OR id NOT IN (SELECT id FROM events WHERE workspace=? ORDER BY id DESC LIMIT 5000))',(workspace,now-14*DAY,workspace))
        db.execute('DELETE FROM tracks WHERE workspace=? AND track_id NOT IN (SELECT track_id FROM sequences WHERE workspace=? UNION SELECT track_id FROM import_items WHERE workspace=? AND track_id IS NOT NULL)',(workspace,workspace,workspace))
        db.execute('DELETE FROM attributes WHERE workspace=? AND track_id NOT IN (SELECT track_id FROM tracks WHERE workspace=?)',(workspace,workspace))
        active={r[0] for r in db.execute('SELECT DISTINCT source FROM imports')}
        for scope,name,playlist,updated in db.execute('SELECT scope,name,playlist,updated FROM cache').fetchall():
            if scope!='ui' and updated<now-7*DAY and playlist not in active:
                db.execute('DELETE FROM cache WHERE scope=? AND name=?',(scope,name))
        if state.get('status') not in ('pending','creating'):
            backups=db.execute("SELECT name,updated FROM documents WHERE workspace=? AND name LIKE 'history-backup-%.json' ORDER BY updated DESC",(workspace,)).fetchall()
            for index,(name,updated) in enumerate(backups):
                if index>=3 or updated<now-30*DAY:
                    db.execute('DELETE FROM documents WHERE workspace=? AND name=?',(workspace,name))
        db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?)',(workspace,'retention.json',json.dumps({'last_run':now}),now))
    # Removed-setup backups are independent exports, never active workspaces.
    if store.path.parent.name=='.spotify-accounts':
        project=store.path.parent.parent
        directory=project/'.sync-backups'
        if directory.exists():
            backups=sorted((p for p in directory.iterdir() if p.is_dir() and not p.is_symlink()),key=lambda p:p.stat().st_mtime,reverse=True)
            for index,p in enumerate(backups):
                if index>=10 or p.stat().st_mtime<now-30*DAY:shutil.rmtree(p)
        # Only expendable HTTP/source cache files, never tokens or cooldowns.
        cache=store.path.parent/'cache'
        if cache.exists():
            for p in cache.rglob('*.json'):
                if not p.is_symlink() and p.stat().st_mtime<now-7*DAY:p.unlink()
