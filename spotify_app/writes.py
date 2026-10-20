"""Durable per-batch writes. Playback may continue while a saved plan is published."""
from contextlib import nullcontext
from pathlib import Path
from .client import SpotifyError, save_json


def ids(rows):
    return [(r.get('item',r.get('track')) or {}).get('id') for r in rows]


class PreconditionFailed(SpotifyError): pass
class TargetChanged(PreconditionFailed): pass
class PlaybackChanged(PreconditionFailed): pass  # Legacy error-code compatibility.


def guard(client,target,snapshot,events=None,actual=None):
    metadata,rows=client.playlist(target)
    if metadata['snapshot_id'] != snapshot or (actual is not None and ids(rows)!=actual):
        raise TargetChanged('Target changed during planning; no write attempted. Preview again.')
    # Listening changes future sync input; it does not edit this target's snapshot.
    return metadata,rows


def desired_queue(state):
    return state.get('pending_update',state)['queue']


def _commit(path,state,writer):
    update=state.pop('pending_update',None)
    report=state.pop('pending_report',None)
    if update:state.update(update)
    state['status']='ready'
    state.pop('write_guard',None);state.pop('write_journal',None)
    owner=getattr(writer,'__self__',None)
    with owner.transaction() if hasattr(owner,'transaction') else nullcontext():
        writer(path,state)
        if report is not None:writer(Path(path).parent/'decision-log.json',report)


def _batch(client,target,uris,append):
    method=getattr(type(client),'write_batch',None)
    if method:return client.write_batch(target,uris,append=append)
    if append:raise SpotifyError('Client does not support journaled append batches')
    return client.replace(target,uris)


def apply(client,path,state,writer=save_json,progress=None,recovery=False):
    desired=[t['id'] for t in desired_queue(state)]
    metadata,rows=client.playlist(state['target']);actual=ids(rows)
    if metadata['owner']['id']!=state['account']:raise PreconditionFailed('Target ownership mismatch')
    if actual==desired:
        _commit(path,state,writer);return
    journal=state.get('write_journal')
    if journal is None:
        saved=state.get('write_guard')
        if saved:
            unchanged=actual==saved['actual'] and metadata['snapshot_id']==saved['snapshot']
            legacy_prefix=recovery and actual==desired[:len(actual)] and bool(actual)
            if not unchanged and not legacy_prefix:raise TargetChanged('Target diverged from the journal; no write attempted' if recovery else 'Target changed immediately before write; no write attempted')
        else:legacy_prefix=False
        journal=dict(sent=len(actual) if legacy_prefix else 0,uncertain=False,snapshot=metadata['snapshot_id'],before=actual)
        state['write_journal']=journal;writer(path,state)
    while journal['sent']<len(desired) or (not desired and not journal['sent']):
        sent=journal['sent'];expected=desired[:min(sent+100,len(desired))]
        before=journal['before'] if sent==0 else desired[:sent]
        if metadata['owner']['id']!=state['account']:raise PreconditionFailed('Target ownership mismatch')
        if journal['uncertain']:
            if actual==expected:
                journal.update(sent=len(expected),snapshot=metadata['snapshot_id'],uncertain=False);writer(path,state)
                if len(expected)==len(desired):break
                sent=journal['sent'];before=desired[:sent];expected=desired[:min(sent+100,len(desired))]
            elif actual==before and metadata['snapshot_id']==journal['snapshot']:
                journal['uncertain']=False;writer(path,state)
            else:raise PreconditionFailed('Target diverged from the saved write journal; playlist kept unchanged')
        if actual!=before or metadata['snapshot_id']!=journal['snapshot']:
            raise PreconditionFailed('Target diverged from the saved write journal; playlist kept unchanged')
        batch=desired[sent:sent+100]
        journal['uncertain']=True;writer(path,state)
        if progress:progress.emit('publishing',message='Updating Spotify target',completed=sent,total=len(desired))
        _batch(client,state['target'],['spotify:track:'+identity for identity in batch],append=sent>0)
        metadata,rows=client.playlist(state['target']);actual=ids(rows)
        if metadata['owner']['id']!=state['account'] or actual!=expected:
            raise SpotifyError('Readback mismatch; pending state retained')
        journal.update(sent=len(expected),snapshot=metadata['snapshot_id'],uncertain=False);writer(path,state)
        if not batch:break
    if progress:progress.emit('verifying',message='Playlist update verified')
    _commit(path,state,writer)
