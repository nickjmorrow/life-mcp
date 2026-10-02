"""One-time Apple Music sign-in: serves a MusicKit JS page on 127.0.0.1:8770 (APPLEMUSIC_SIGNIN_PORT;
8769 is the flashcard page), he clicks Sign in,
Apple's popup asks him to allow "Life", and the Music user token is saved to
~/.config/life-mcp/applemusic.json (600). Then it exits. Run again when the sign-in expires
(about every 6 months; the tools say so).

    source ~/.zshrc.local && uv run applemusic_signin.py
"""
import html
import json
import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from applemusic_api import AppleMusic, save_user_token

PORT = int(os.environ.get("APPLEMUSIC_SIGNIN_PORT", "8770"))

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Life: Apple Music sign-in</title>
<style>body{font:16px -apple-system,sans-serif;background:#0b1a33;color:#f5f1e3;display:grid;place-items:center;min-height:100vh;margin:0}
main{max-width:30rem;padding:2rem}button{font:inherit;padding:.8rem 1.4rem;border:0;border-radius:.6rem;background:#ffd23f;color:#0b1a33;cursor:pointer}
#s{margin-top:1rem;opacity:.85}</style>
<script src="https://js-cdn.music.apple.com/musickit/v3/musickit.js" async></script></head>
<body><main><h1>Apple Music for Life</h1>
<p>Lets Claude read your listening history, recommendations and library, and add music for you.</p>
<button id="b" disabled>Sign in to Apple Music</button><p id="s">Loading MusicKit…</p></main>
<script>
const DEV = __DEV__, NONCE = __NONCE__;
const s = document.getElementById("s"), b = document.getElementById("b");
document.addEventListener("musickitloaded", async () => {
  try {
    await MusicKit.configure({developerToken: DEV, app: {name: "Life", build: "1.0"}});
    b.disabled = false; s.textContent = "Ready.";
  } catch (e) { s.textContent = "MusicKit didn't start: " + e; }
});
b.onclick = async () => {
  try {
    s.textContent = "Waiting for Apple…";
    const token = await MusicKit.getInstance().authorize();
    const r = await fetch("/token", {method: "POST", headers: {"Content-Type": "application/json"},
                                     body: JSON.stringify({token, nonce: NONCE})});
    s.textContent = r.ok ? "Done. Life is signed in; you can close this tab." : "Saving failed: " + await r.text();
  } catch (e) { s.textContent = "Sign-in didn't finish: " + e; }
};
</script></body></html>"""


def main() -> None:
    am = AppleMusic()
    nonce = secrets.token_urlsafe(24)
    page = PAGE.replace("__DEV__", json.dumps(am.dev_token())).replace("__NONCE__", json.dumps(nonce))
    done = {"ok": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _local(self) -> bool:
            """Only this page at this address (no DNS-rebinding page can read the token or post one)."""
            if self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
                return True
            self.send_error(403)
            return False

        def do_GET(self):
            if not self._local():
                return
            if self.path != "/":
                self.send_error(404)
                return
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self._local():
                return
            if self.path != "/token":
                self.send_error(404)
                return
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except ValueError:
                data = {}
            if data.get("nonce") != nonce or not isinstance(data.get("token"), str) or len(data["token"]) < 20:
                self.send_error(400, "bad request")
                return
            save_user_token(data["token"])
            done["ok"] = True
            self.send_response(204)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Open {html.escape(url)} and sign in (opening it now).", flush=True)
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    while not done["ok"]:
        server.handle_request()
    print("Signed in; token saved to ~/.config/life-mcp/applemusic.json.")


if __name__ == "__main__":
    main()
