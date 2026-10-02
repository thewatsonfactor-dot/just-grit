# -*- coding: utf-8 -*-
"""The picture library: the customer's own marketing material, ready to post.

Until now the only way to get a picture into a post was to generate one.
Every business already has a folder of real photos, a logo, a flyer - on
a laptop, an external drive, a Google Drive, a Dropbox - and Buffer, Later
and Metricool all let you pull from it. Daniel's rule: the ORIGINALS stay
wherever the customer keeps them. The app takes a copy of what gets used,
because Meta will only fetch a picture from a public URL and a shared Drive
link is not one.

Two ways in, both without a Google sign-in:

  * a file from the computer (or a phone, or an external drive) - the
    browser reads it and posts the bytes;
  * a link - a Google Drive folder shared "anyone with the link", a single
    Drive file, a Dropbox share link, or a plain image URL.

Everything network-facing takes an injected `http(url) -> (status, headers,
bytes)` so the parsing is testable with nothing on the wire.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlparse, parse_qs, urlencode

IMAGE_TYPES = {"image/jpeg", "image/png"}
MAX_FILES = 60          # a folder with 400 photos is a mistake, not a request

DRIVE_FOLDER_RE = re.compile(r"drive\.google\.com/(?:drive/(?:u/\d+/)?folders/|embeddedfolderview\?id=)([A-Za-z0-9_-]{10,})")
DRIVE_FILE_RE = re.compile(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=\w+&)?id=)([A-Za-z0-9_-]{10,})")
DROPBOX_RE = re.compile(r"^https?://(?:www\.)?dropbox\.com/", re.I)


class LibraryError(RuntimeError):
    pass


def classify(link: str) -> tuple:
    """What kind of link is this? -> (kind, ref)

    kind is one of drive_folder, drive_file, dropbox, url. Anything that
    is not an http(s) link is refused here, with the reason a person can
    act on.
    """
    s = (link or "").strip()
    if not s:
        raise LibraryError("Paste a link first.")
    if not re.match(r"^https?://", s, re.I):
        raise LibraryError("That doesn't look like a web link. It should start with https://")
    m = DRIVE_FOLDER_RE.search(s)
    if m:
        return "drive_folder", m.group(1)
    m = DRIVE_FILE_RE.search(s)
    if m:
        return "drive_file", m.group(1)
    if DROPBOX_RE.match(s):
        return "dropbox", s
    return "url", s


def drive_folder_html_url(folder_id: str) -> str:
    """Google's embeddable folder view - plain HTML, no key, works for any
    folder shared 'anyone with the link'."""
    return "https://drive.google.com/embeddedfolderview?id=%s" % folder_id


def drive_folder_api_url(folder_id: str, key: str) -> str:
    q = "'%s' in parents and trashed=false" % folder_id
    return "https://www.googleapis.com/drive/v3/files?" + urlencode(
        {"q": q, "fields": "files(id,name,mimeType,size)", "pageSize": 200, "key": key})


def drive_download_url(file_id: str, key: str = "") -> str:
    if key:
        return "https://www.googleapis.com/drive/v3/files/%s?alt=media&key=%s" % (file_id, key)
    return "https://drive.google.com/uc?export=download&id=%s" % file_id


def parse_drive_folder_html(html: str) -> list:
    """The embedded folder view lists every entry as
       <div class="flip-entry" id="entry-<FILEID>"> ... <div class="flip-entry-title">name</div>
    Returns [{"id","name"}] in page order. Sub-folders show up too and are
    filtered by the caller on extension, since the HTML carries no type."""
    out = []
    for m in re.finditer(r'id="entry-([A-Za-z0-9_-]+)"(.*?)flip-entry-title">(.*?)</div>', html, re.S):
        out.append({"id": m.group(1), "name": unescape(m.group(3)).strip()})
    return out


def parse_drive_folder_api(payload: dict) -> list:
    return [{"id": f.get("id", ""), "name": f.get("name", ""), "mime": f.get("mimeType", "")}
            for f in (payload or {}).get("files", []) if f.get("id")]


def looks_like_image_name(name: str) -> bool:
    return bool(re.search(r"\.(jpe?g|png)$", name or "", re.I))


def dropbox_direct(link: str) -> str:
    """A Dropbox share link shows a web page; dl=1 on the same link serves
    the file. raw=1 also works for images but dl=1 covers both."""
    u = urlparse(link)
    q = parse_qs(u.query)
    q["dl"] = ["1"]
    return u._replace(query=urlencode({k: v[0] for k, v in q.items()})).geturl()


def is_html(headers: dict, body: bytes) -> bool:
    ct = ""
    for k, v in (headers or {}).items():
        if k.lower() == "content-type":
            ct = (v or "").lower()
    if "text/html" in ct:
        return True
    head = (body or b"")[:300].lower()
    return head.lstrip().startswith(b"<!doctype html") or b"<html" in head


def list_link(link: str, http, google_key: str = "") -> dict:
    """Resolve a pasted link to the pictures behind it.

    Returns {"kind", "items": [{"name", "download", "ref"}], "note"}. Nothing
    is downloaded here - a folder of sixty photos is listed first so the
    caller can decide, and the download step is separate and per-file.
    """
    kind, ref = classify(link)
    if kind == "drive_folder":
        items, note = [], ""
        if google_key:
            status, _, body = http(drive_folder_api_url(ref, google_key))
            if status == 200:
                import json
                try:
                    for f in parse_drive_folder_api(json.loads(body.decode("utf-8", "replace"))):
                        if f["mime"] in IMAGE_TYPES or looks_like_image_name(f["name"]):
                            items.append({"name": f["name"], "ref": f["id"],
                                          "download": drive_download_url(f["id"], google_key)})
                except ValueError:
                    pass
        if not items:
            status, headers, body = http(drive_folder_html_url(ref))
            if status != 200:
                raise LibraryError("Google answered %s for that folder. Is it shared "
                                   "as 'Anyone with the link'? (Right-click the folder in "
                                   "Drive → Share → General access.)" % status)
            entries = parse_drive_folder_html(body.decode("utf-8", "replace"))
            items = [{"name": e["name"], "ref": e["id"], "download": drive_download_url(e["id"])}
                     for e in entries if looks_like_image_name(e["name"])]
            if not entries:
                note = ("The folder came back empty. If it has pictures in it, the share "
                        "setting is probably 'Restricted' - change it to 'Anyone with the link'.")
        if len(items) > MAX_FILES:
            note = ("Only the first %d pictures were taken - that folder has %d. Make a "
                    "smaller 'for social' folder if you want a specific set." % (MAX_FILES, len(items)))
            items = items[:MAX_FILES]
        return {"kind": kind, "items": items, "note": note}
    if kind == "drive_file":
        return {"kind": kind, "note": "",
                "items": [{"name": "drive-" + ref[:8], "ref": ref,
                           "download": drive_download_url(ref, google_key)}]}
    if kind == "dropbox":
        name = urlparse(ref).path.rstrip("/").split("/")[-1] or "dropbox"
        return {"kind": kind, "note": "",
                "items": [{"name": name, "ref": ref, "download": dropbox_direct(ref)}]}
    name = urlparse(ref).path.rstrip("/").split("/")[-1] or "picture"
    return {"kind": kind, "note": "", "items": [{"name": name, "ref": ref, "download": ref}]}


def fetch_image(download_url: str, http) -> bytes:
    """One picture's bytes, or a reason. Google's download endpoint hands
    back an HTML 'can't scan for viruses' page for big files and a sign-in
    page for private ones; both are HTML, neither is a picture."""
    status, headers, body = http(download_url)
    if status in (401, 403, 404):
        raise LibraryError("That file isn't shared publicly (Google answered %s). Share it "
                           "as 'Anyone with the link' and try again." % status)
    if status != 200:
        raise LibraryError("Could not download it (HTTP %s)." % status)
    if is_html(headers, body):
        raise LibraryError("The link opened a web page instead of a picture. For Google "
                           "Drive, make sure the file or folder is shared as 'Anyone with "
                           "the link'; for other sites, paste the picture's own address.")
    if not body:
        raise LibraryError("The download was empty.")
    return body


def clean_name(name: str) -> str:
    n = re.sub(r"[\r\n\t]", " ", (name or "")).strip()
    return n[:120] or "picture"
