"""Does this domain's mail actually authenticate?

Missing SPF/DKIM/DMARC is the most common silent deliverability failure: the
send succeeds, the app says "sent", and the message lands in spam. Nothing in
Just Grit could see that until now.

The rules below are ported from Warmbly's `internal/pkg/dnsauth` package
(github.com/warmbly/warmbly, Apache License 2.0, (c) 2026 Mindroot Ltd).
This is a Python reimplementation of their logic, not a copy of their source.
Three judgements of theirs are worth keeping and are easy to get wrong:

  * A lookup that ERRORS is "unknown", never "failing". A resolver hiccup must
    never read as a misconfigured domain and stop your sending.
  * DKIM is advisory. Selectors are not discoverable from DNS, so "we probed
    nine common selectors and found none" is not proof of absence.
  * DMARC p=none counts as present. Google's bulk-sender rules require a
    record, not a strict policy - so p=none is compliant, just not protective.
"""
from __future__ import annotations

import re
from typing import Optional

# Host-specific selectors matter more than generic ones. Namecheap Private
# Email uses "default" on some accounts and "privateemail" on others - probing
# only the generic list reported a correctly published key as missing.
COMMON_SELECTORS = ["default", "privateemail", "google", "selector1", "selector2",
                    "k1", "k2", "mail", "dkim", "s1", "s2", "zoho", "mandrill",
                    "sendgrid", "mailjet", "pm", "protonmail", "fm1", "smtp"]

# Special-use names that are defined never to resolve. They cannot be
# evaluated, which is different from failing evaluation.
RESERVED = re.compile(r"\.(test|invalid|localhost|example|local)$", re.I)


# The first version of this trusted whatever resolver the Mac was configured
# with, and that resolver returned "no record" for _dmarc.google.com - a domain
# that certainly has one. Reporting that as "your domain is misconfigured"
# is the exact failure the rules above warn about, so the module now proves
# its own resolver works before it believes an absence.
PUBLIC_RESOLVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]

# Must exist. If a resolver can't see this, it can't be trusted to say a
# record is missing.
CONTROL_NAME = "_dmarc.google.com"


def dkim_key_usable(record: str):
    """A DKIM record existing is not the same as a DKIM key working.

    homerepair.tech had a published, correctly-named record whose public key
    was missing one character - `p=MIBIjAN...` instead of `p=MIIBIjAN...`. It
    looked healthy to every "is there a record?" check, including this one's
    first version, while every signature it produced failed verification for
    months. So: decode the key and load it, or say why not.

    Returns (usable, reason, key_bits).
    """
    import base64
    tags = dict(re.findall(r"(\w+)\s*=\s*([^;]*)", record or ""))
    p = (tags.get("p") or "").strip().replace(" ", "")
    if not p:
        return False, "the record has no public key (empty p= tag)", 0
    if (tags.get("k") or "rsa").lower() not in ("rsa", "ed25519"):
        return False, "unrecognised key type k=%s" % tags.get("k"), 0
    try:
        der = base64.b64decode(p, validate=True)
    except Exception:
        return False, ("the public key is not valid base64 - it is corrupted or "
                       "was truncated when it was pasted in"), 0
    try:
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        k = load_der_public_key(der)
        bits = getattr(k, "key_size", 0)
        if bits and bits < 1024:
            return False, "the key is only %d-bit; receivers ignore anything under 1024" % bits, bits
        return True, "", bits
    except ImportError:
        # No crypto library: check the DER header a 2048-bit RSA SPKI must have.
        if der[:2] != b"\x30\x82":
            return False, "the public key does not parse as a key", 0
        return True, "", (len(der) - 38) * 8
    except Exception:
        return False, ("the public key decodes but is not a usable key - "
                       "receivers cannot verify anything signed with it"), 0


def is_dmarc(record: str) -> bool:
    """A DMARC record starts with v=DMARC1, in whatever case the operator
    happened to type. Compare case-insensitively on BOTH sides - the first
    version of this asked whether "v=DMARC1" appeared in record.upper(), which
    is never true, so every domain on earth read as having no DMARC."""
    return (record or "").strip().upper().startswith("V=DMARC1")


def _resolver(nameservers=None):
    import dns.resolver
    r = dns.resolver.Resolver(configure=nameservers is None)
    if nameservers:
        r.nameservers = list(nameservers)
    r.lifetime = 6.0
    r.timeout = 3.0
    return r


def _trustworthy(res) -> bool:
    recs, err = _txt(res, CONTROL_NAME)
    return (not err) and any(is_dmarc(t) for t in recs)


def working_resolver():
    """The system resolver if it can be trusted, otherwise a public one.
    Returns (resolver, note) - note is set when we had to fall back."""
    try:
        sysr = _resolver()
        if _trustworthy(sysr):
            return sysr, ""
    except Exception:
        pass
    for ns in PUBLIC_RESOLVERS:
        try:
            r = _resolver([ns])
            if _trustworthy(r):
                return r, ("This machine's DNS couldn't resolve a record that "
                           "definitely exists, so the check ran against %s "
                           "instead." % ns)
        except Exception:
            continue
    return None, ("No working DNS resolver - neither this machine's nor a public "
                  "one could answer a control lookup. Nothing below is reliable.")


def _txt(res, name):
    """Returns (records, errored). errored=True only for a real failure -
    NXDOMAIN and NoAnswer both mean 'no record', which is an answer."""
    import dns.resolver
    try:
        return [b"".join(r.strings).decode(errors="replace")
                for r in res.resolve(name, "TXT")], False
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return [], False
    except Exception:
        return [], True


def _org_domain(domain: str) -> str:
    """Crude organizational-domain fallback: DMARC on example.com covers
    mail.example.com (RFC 7489 6.6.3). Good enough for two-label public
    suffixes, which is every domain in this pipeline."""
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else domain


def check_domain_auth(domain: str, selectors: Optional[list] = None) -> dict:
    domain = (domain or "").strip().lower().rstrip(".")
    out = {"domain": domain, "spf": None, "dkim": [], "dkim_broken": [],
           "dkim_bits": 0, "dmarc": None,
           "dmarc_policy": "", "dmarc_inherited": False, "mx": [],
           "state": "unknown", "lookup_error": False, "notes": []}
    if not domain:
        out["notes"].append("No domain given.")
        return out
    if RESERVED.search(domain):
        out["notes"].append("Special-use domain - it is defined never to resolve.")
        return out

    res, resolver_note = working_resolver()
    if res is None:
        out["lookup_error"] = True
        out["notes"].append(resolver_note)
        return out
    if resolver_note:
        out["notes"].append(resolver_note)

    try:
        out["mx"] = sorted(r.to_text() for r in res.resolve(domain, "MX"))
    except Exception:
        pass

    out["dns_host"], out["nameservers"] = dns_host(res, domain)

    txt, err = _txt(res, domain)
    spf = [t for t in txt if t.lower().startswith("v=spf1")]
    out["spf"] = spf[0] if spf else None
    if err:
        out["lookup_error"] = True

    # DMARC: the domain's own record, else the organizational domain's.
    dm, derr = _txt(res, "_dmarc." + domain)
    rec = [t for t in dm if is_dmarc(t)]
    if not rec and _org_domain(domain) != domain:
        dm2, derr2 = _txt(res, "_dmarc." + _org_domain(domain))
        rec = [t for t in dm2 if is_dmarc(t)]
        derr = derr or derr2
        if rec:
            out["dmarc_inherited"] = True
    if derr:
        out["lookup_error"] = True
    if rec:
        out["dmarc"] = rec[0]
        m = re.search(r"\bp\s*=\s*([a-z]+)", rec[0], re.I)
        out["dmarc_policy"] = (m.group(1).lower() if m else "")

    for sel in (selectors or COMMON_SELECTORS):
        recs, _ = _txt(res, "%s._domainkey.%s" % (sel, domain))
        rec = next((r for r in recs if r.strip()), "")
        if not rec:
            continue
        ok, why, bits = dkim_key_usable(rec)
        out["dkim"].append(sel)
        if not ok:
            out["dkim_broken"].append({"selector": sel, "why": why})
        elif bits:
            out["dkim_bits"] = bits

    # The verdict. SPF and DMARC are discoverable, so their absence is real.
    # DKIM's is not, so it can never force a failure on its own.
    if out["lookup_error"] and not (out["spf"] and out["dmarc"]):
        out["state"] = "unknown"
        out["notes"].append("A DNS lookup failed, so this is inconclusive rather "
                            "than a problem with the domain. Try again shortly.")
    elif out["spf"] and out["dmarc"]:
        out["state"] = "passing"
    else:
        out["state"] = "failing"

    if not out["spf"]:
        out["notes"].append("No SPF record. Receivers can't tell which servers "
                            "are allowed to send as you.")
    if not out["dmarc"]:
        out["notes"].append("No DMARC record. Google and Yahoo both require one "
                            "for bulk senders.")
    elif out["dmarc_policy"] == "none":
        out["notes"].append("DMARC is set to p=none, which only monitors. That "
                            "meets the requirement but protects nothing yet - "
                            "move to quarantine once the reports look clean.")
    if out["dkim_broken"]:
        for b in out["dkim_broken"]:
            out["notes"].append(
                "DKIM selector '%s' is PUBLISHED BUT BROKEN: %s. Every message "
                "signed with it fails verification. Regenerate the key with your "
                "mail host and republish it." % (b["selector"], b["why"]))
        # A record that exists but cannot be used is worse than none: it looks
        # healthy on every dashboard while silently failing.
        if out["state"] == "passing":
            out["state"] = "failing"

    if not out["dkim"]:
        where = (" Your DNS is served by %s, so the record has to be added there - "
                 "not at whoever you bought the domain from, and not at your mail "
                 "host." % out["dns_host"]) if out["dns_host"] else ""
        out["notes"].append(
            "No DKIM found on the common selectors. That is not proof it's "
            "missing - selectors can't be discovered from DNS - but if your mail "
            "host generated a DKIM key and nobody published it, this is the gap "
            "to close first." + where)
    out["out_key"] = None
    out.pop("out_key")
    return out


# Where a domain's DNS is actually served. This is the single most common way
# a DKIM record never gets published: the mail host generates the key and says
# "add this at your DNS provider", and the operator adds it at the registrar -
# which is not the DNS provider when the nameservers point somewhere else.
NS_PROVIDERS = [
    ("cloudflare.com",        "Cloudflare"),
    ("registrar-servers.com", "Namecheap BasicDNS"),
    ("namecheaphosting.com",  "Namecheap"),
    ("domaincontrol.com",     "GoDaddy"),
    ("awsdns",                "AWS Route 53"),
    ("googledomains.com",     "Google Domains"),
    ("dnsimple.com",          "DNSimple"),
    ("digitalocean.com",      "DigitalOcean"),
    ("vercel-dns.com",        "Vercel"),
    ("azure-dns",             "Azure DNS"),
]


def dns_host(res, domain: str):
    """Returns (provider_label, nameservers)."""
    try:
        ns = sorted(r.to_text().rstrip(".").lower() for r in res.resolve(domain, "NS"))
    except Exception:
        return "", []
    for needle, label in NS_PROVIDERS:
        if any(needle in n for n in ns):
            return label, ns
    return "", ns


def sending_domain(addr: str) -> str:
    return addr.split("@")[-1].strip().lower() if "@" in (addr or "") else ""
