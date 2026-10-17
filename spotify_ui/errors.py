"""Stable product error descriptors; diagnostic messages remain available to logs/CLI."""
import re

MESSAGES={
 'network':'The connection was interrupted. Your mix is saved. Reconnect and resume the update.',
 'target_missing':'This playlist was removed. Recreate it to keep using this mix.',
 'unexpected':'Something went wrong. Try again; your saved mix is still available.',
 'history_gap':'Spotify history no longer covers the saved bookmark. Preview recovery to continue.',
 'target_changed':'This playlist was edited in Spotify. Preview reconciliation before applying changes.',
 'authentication':'Reconnect Spotify to keep this mix updated.',
 'unavailable':'A playlist is no longer available. Check your target and source playlists in Spotify.',
 'cooldown':'Waiting for Spotify until {deadline}. Your playlist and saved progress are safe.',
 'rate_limit':'Spotify has asked us to wait. Your progress is saved; sync will resume after the cooldown.',
 'preview_expired':'This preview expired. Preview the sync again.',
 'playback_changed':'Listening changed while syncing. Your playlist is safe; try again shortly.',
 'busy':'An operation is already running. Wait for it to finish or pause it.',
 'mix_missing':'This mix was not found. Refresh the page.',
 'invalid_sources':'Select at least one valid source playlist.',
 'generated_source':'Generated playlists cannot be selected as sources.',
 'finish_setup':'Finish or recover setup before editing this mix.',
 'invalid_rules':'Choose valid track, artist and album spacing.',
 'invalid_name':'Choose a playlist name of 1–100 characters.',
 'invalid_buffer':'Choose valid initial and normal queue sizes.',
 'invalid_request':'The request could not be accepted. Refresh the app and try again.',
 'account_mismatch':'This mix belongs to a different Spotify account. Select the correct account.',
 'permission':'Spotify did not allow this action. Check playlist access and reconnect if needed.',
 'cancelled':'The operation was paused. Your saved progress is available.',
 'dependency':'These source playlists form a loop. Choose different sources.',
 'pending_write':'The last update needs verification. Recover it before syncing again.',
 'genre_metadata':'Genre spacing needs complete labels. Add labels or disable genre spacing.',
}

PATTERNS=[
 ('network',r'connection was interrupted|Network failure'),
 ('history_gap',r'Playback history gap'),('target_changed',r'Target changed externally|Target diverged'),
 ('authentication',r'HTTP 401|Run login first|Choose --account'),('unavailable',r'HTTP 404'),
 ('permission',r'HTTP 403|ownership mismatch'),('cooldown',r'Waiting for Spotify until'),
 ('rate_limit',r'rate limit|temporarily limiting|cooldown saved'),('preview_expired',r'preview expired'),
 ('playback_changed',r'Playback changed|Playlist changed'),('busy',r'already running|mix is busy'),
 ('mix_missing',r'mix was not found'),('generated_source',r'Generated playlists'),
 ('invalid_sources',r'valid source|source is required'),('finish_setup',r'Finish.*setup|Finish or recover'),
 ('invalid_rules',r'valid track|spacing|strength'),('invalid_name',r'playlist name'),
 ('invalid_buffer',r'buffer|queue size'),('account_mismatch',r'different account|does not match this login'),
 ('dependency',r'circular|cycle'),('pending_write',r'Incomplete write|pending|journal'),
 ('genre_metadata',r'Genre rules require'),
]

def describe(error):
    raw=str(error)
    code=getattr(error,'ui_code',None) or {401:'authentication',403:'permission',404:'unavailable',429:'rate_limit'}.get(getattr(error,'status',None))
    if code not in MESSAGES:
        code=next((code for code,pattern in PATTERNS if re.search(pattern,raw,re.I)), 'unexpected')
    return dict(code=code,params=getattr(error,'ui_params',{}))
