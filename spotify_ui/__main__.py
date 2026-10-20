import argparse
from pathlib import Path
from .controller import Controller
from .server import Server

def main():
    parser=argparse.ArgumentParser(description='Smart playlist local web app')
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args()
    controller=Controller(args.project)
    server=Server(('127.0.0.1',args.port),controller)
    print('Smart playlists: '+server.origin,flush=True)
    try:server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:pass
    finally:
        server.server_close();controller.close()
if __name__=='__main__':main()
