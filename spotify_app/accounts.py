"""Shared local accounts; credentials remain owner-readable files, outside music data."""
import hashlib
import fcntl
from pathlib import Path
from .client import read_json, save_json, SpotifyError


def fingerprint(token):
    return hashlib.sha256((token['client_id']+token.get('refresh_token',token['access_token'])).encode()).hexdigest()


class Accounts:
    def __init__(self, root):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.index_path=self.root/'accounts.json'

    def select(self, workspace, account=None):
        index=read_json(self.index_path,{})
        local=Path(workspace)/'token.json'
        if local.exists() and (account is None or account not in index):
            token=read_json(local)
            for row in index.values():
                if row['fingerprint']==fingerprint(token):
                    return self.root/row['token_file']
            return local
        if account is None:
            # Prefer the account bound to this workspace's legacy setup.
            for name in ('state.json','setup.json'):
                state=read_json(Path(workspace)/name)
                if state and state.get('account') in index:
                    account=state['account'];break
        if account is None and len(index)==1:
            account=next(iter(index))
        if account not in index:
            raise SpotifyError('Choose --account ACCOUNT_ID or log in; multiple accounts are not selected implicitly')
        return self.root/index[account]['token_file']

    def register(self, account, token):
        with (self.root/'accounts.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return self._register(account,token)

    def _register(self, account, token):
        index=read_json(self.index_path,{})
        filename=hashlib.sha256(account.encode()).hexdigest()+'.json'
        save_json(self.root/filename,token)
        index[account]=dict(token_file=filename,fingerprint=fingerprint(token),client_id=token['client_id'])
        save_json(self.index_path,index)
        return self.root/filename

    def rate_path(self,token):
        return self.root/('rate-'+hashlib.sha256(token['client_id'].encode()).hexdigest()+'.json')

    def import_legacy_rate(self,workspace,token):
        path=self.rate_path(token)
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            legacy=read_json(Path(workspace)/'rate.json',{})
            shared=read_json(path,{})
            if legacy.get('next',0)>shared.get('next',0):
                save_json(path,dict(shared,next=legacy['next']))
        return path
