from urllib.parse import parse_qs,urlparse

class FakeSpotify:
    def __init__(self):
        self.sources={
            'SourceA':dict(name='Late night discoveries',ids=list(range(180))),
            'SourceB':dict(name='Electronic essentials',ids=list(range(90,270))),
            'SourceC':dict(name='Weekend favourites',ids=list(range(180,360)))}
        self.targets={};self.names={};self.version={};self.events=[];self.metrics={}
        self.token={'client_id':'a'*32,'access_token':'synthetic-access','refresh_token':'synthetic-refresh'}
    def raw(self,i):
        titles=['Afterglow','Night drive','Slow orbit','Soft signals','Blue hour','Open sky']
        return dict(type='track',id='Track'+str(i),name=titles[i%6]+' '+str(i+1),artists=[dict(id='Artist'+str(i%60),name='Artist '+str(i%60+1))],album=dict(id='Album'+str(i//6)),uri='spotify:track:Track'+str(i))
    def request(self,method,path,data=None):
        if path=='/me':return dict(id='test-account',display_name='Test listener')
        if method=='POST' and path=='/me/playlists':
            identity='TestMix'+str(len(self.targets)+1);self.targets[identity]=[];self.version[identity]=0
            return {'id':identity}
        parsed=urlparse(path);identity=parsed.path.split('/')[2]
        if method=='PUT':
            self.names[identity]=data['name'];return {}
        if parsed.path.endswith('/items'):
            ids=self.sources[identity]['ids'] if identity in self.sources else self.targets[identity]
            query=parse_qs(parsed.query);offset=int(query.get('offset',[0])[0]);limit=int(query.get('limit',[50])[0])
            return dict(items=[{'item':self.raw(i)} for i in ids[offset:offset+limit]],total=len(ids),next=f'/playlists/{identity}/items?limit={limit}&offset={offset+limit}' if offset+limit<len(ids) else None)
        return dict(snapshot_id=str(self.version.get(identity,0)),owner={'id':'test-account'},name=self.names.get(identity,(self.sources.get(identity) or {}).get('name','Test mix')),collaborative=False)
    def pages(self,path):
        for identity,source in self.sources.items():yield dict(id=identity,name=source['name'],owner={'id':'test-account'},items={'total':len(source['ids'])})
    def playlist(self,identity):
        ids=self.sources[identity]['ids'] if identity in self.sources else self.targets[identity]
        return self.request('GET','/playlists/'+identity),[{'item':self.raw(i)} for i in ids]
    source_playlist=playlist
    def recent(self):return list(self.events)
    def write_batch(self,identity,uris,append=False):
        if not append:return self.replace(identity,uris)
        self.targets[identity].extend(int(uri.split('Track')[-1]) for uri in uris);self.version[identity]+=1
    def replace(self,identity,uris):
        self.targets[identity]=[int(uri.split('Track')[-1]) for uri in uris];self.version[identity]+=1
    def invalidate_playlist(self,identity):pass
