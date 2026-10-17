import argparse,tempfile
from pathlib import Path
from .controller import Controller
from .server import Server

def main():
    parser=argparse.ArgumentParser(description='Smart playlist local web app')
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--demo',action='store_true',help='Use synthetic data in a temporary workspace; never contact Spotify')
    args=parser.parse_args()
    temp=tempfile.TemporaryDirectory(prefix='spotify-ui-demo-') if args.demo else None
    controller=Controller(Path(temp.name) if temp else args.project,demo=args.demo)
    server=Server(('127.0.0.1',args.port),controller)
    print('Smart playlists: '+server.origin,flush=True)
    if args.demo:print('Demo mode: synthetic music only; no Spotify writes.',flush=True)
    try:server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:pass
    finally:
        server.server_close();controller.close()
        if temp:temp.cleanup()
if __name__=='__main__':main()
