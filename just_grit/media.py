# -*- coding: utf-8 -*-
"""Media hosting, because Instagram will not accept a file.

Meta's publishing APIs do not take an upload. You hand them a URL and their
servers fetch it. That makes "somewhere public to put a JPEG" a hard
prerequisite for social publishing, not a nice-to-have - and it is the piece
people discover last, usually after the rest is built.

Two decisions worth stating:

CONTENT-ADDRESSED. The filename is the SHA-256 of the bytes. Uploading the
same image twice is free and idempotent, a URL can never point at different
content later, and there is no filename to sanitise because we generate it.

THE URL MUST BE ANONYMOUS. Facebook's fetcher has no session. Right now both
tunnel hostnames sit behind Cloudflare Access, so a fetch returns a sign-in
page and the post fails with a useless error. `hosting_problem()` checks for
exactly that and says so in words, rather than letting it surface as
"Instagram could not process the media".
"""
from __future__ import annotations
import hashlib, io, os, re
from pathlib import Path

# Only formats the networks actually accept. Instagram takes JPEG and nothing
# else for images - a PNG is rejected after upload, not before.
KINDS = {
    "image/jpeg": (".jpg", {"JPEG"}),
    "image/png":  (".png", {"PNG"}),
    "video/mp4":  (".mp4", None),
}
EXT_TYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".mp4": "video/mp4"}
MAX_BYTES = 60 * 1024 * 1024
ID_RE = re.compile(r"^[0-9a-f]{64}\.(jpg|png|mp4)$")     # what we generate, exactly


class MediaError(RuntimeError):
    pass


def store_dir(data_root) -> Path:
    d = Path(data_root) / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sniff(raw: bytes) -> str:
    """Trust the bytes, never the client's content-type or file extension."""
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[4:12] in (b"ftypisom", b"ftypmp42", b"ftypMSNV", b"ftypM4V ") or raw[4:8] == b"ftyp":
        return "video/mp4"
    raise MediaError("That file is not a JPEG, PNG or MP4. Instagram takes JPEG "
                     "for images; PNG is rejected by Meta after upload, so it is "
                     "refused here where the message is still useful.")


def save(raw: bytes, data_root) -> dict:
    if not raw:
        raise MediaError("Empty file.")
    if len(raw) > MAX_BYTES:
        raise MediaError("File is %.1f MB; the ceiling here is %d MB."
                         % (len(raw) / 1e6, MAX_BYTES // (1024 * 1024)))
    kind = sniff(raw)
    ext, _ = KINDS[kind]
    name = hashlib.sha256(raw).hexdigest() + ext
    path = store_dir(data_root) / name
    if not path.exists():                      # content-addressed: identical bytes
        path.write_bytes(raw)                  # are the same file, written once
    out = {"id": name, "kind": kind, "bytes": len(raw)}
    out.update(dimensions(raw, kind))
    return out


def dimensions(raw: bytes, kind: str) -> dict:
    if not kind.startswith("image/"):
        return {}
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        w, h = im.size
        return {"width": w, "height": h, "aspect": round(w / h, 4) if h else None}
    except Exception:
        return {}                              # a missing dimension is not a failure


def resolve(name: str, data_root) -> tuple[Path, str]:
    """Turn a URL segment into a file, or refuse.

    The name is matched against the exact shape we generate - 64 hex characters
    and a known extension. Nothing derived from a request is ever joined onto a
    directory and hoped for; that is how a media route becomes a file server for
    the whole disk.
    """
    if not ID_RE.match(name or ""):
        raise MediaError("No such media.")
    path = store_dir(data_root) / name
    if not path.is_file():
        raise MediaError("No such media.")
    return path, EXT_TYPE[path.suffix.lower()]


# The public path. It USED to be /media/<name>, and on the hostnames behind
# Cloudflare Access that path answers a sign-in page to Meta's fetcher - so
# every generated picture failed to post. /welcome/ is already bypassed in
# Access (the landing page and its link-preview image live there), and a
# bypass is a prefix match, so /welcome/m/<name> rides through with it. The
# old /media/ route still serves the file for the app's own thumbnails.
PUBLIC_PREFIX = "/welcome/m/"
FETCHER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"


def public_url(base: str, name: str) -> str:
    return "%s%s%s" % ((base or "").rstrip("/"), PUBLIC_PREFIX, name)


def hosting_problem(base_url: str, fetch) -> str:
    """Can an anonymous stranger actually GET a media URL? Empty string = yes.

    `fetch` takes a url and returns (status, text). Injected so this is
    testable without the network.
    """
    if not base_url:
        return ("No public base URL is set. Meta fetches media from a URL - it "
                "will not accept an upload - so social publishing cannot work "
                "until there is a hostname that serves /media/ to the public.")
    probe = public_url(base_url, "0" * 64 + ".jpg")
    try:
        status, text = fetch(probe)
    except Exception as e:
        return "Could not reach %s (%s)." % (base_url, type(e).__name__)
    low = (text or "").lower()
    if "cloudflare access" in low or "sign in" in low[:2000]:
        return ("%s is behind Cloudflare Access, so Facebook's fetcher gets a "
                "sign-in page instead of the image and the post fails with an "
                "unhelpful error. Add an Access policy of action Bypass for the "
                "path /welcome/* on this hostname - the rest of the app stays "
                "protected." % base_url)
    if status in (301, 302, 303, 307, 308):
        return ("%s sends anonymous visitors to a sign-in page (Cloudflare Access), so "
                "Facebook and Instagram can't fetch your pictures and every post fails. In "
                "Cloudflare Zero Trust, add a Bypass policy for the path /welcome/* on "
                "this hostname - the rest of the app stays protected." % base_url)
    if status == 404:
        return ""                              # reached us, no such file: correct
    if status in (401, 403):
        return ("%s answered %d to an anonymous request. Meta has no session; "
                "the /welcome/ path has to be reachable without logging in."
                % (base_url, status))
    return ""
