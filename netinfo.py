"""Find this computer's LAN addresses so a phone can open the app."""
import socket


def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1)); ips.append(s.getsockname()[0]); s.close()   # no packet is sent
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except Exception:
        pass
    out = []
    for ip in ips:
        if ip.startswith(("127.", "169.254.", "0.")) or ip in out: continue
        out.append(ip)
    return out


def qr_svg(text):
    try:
        import segno, io
        buf = io.BytesIO(); segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#e8edf2", light=None, xmldecl=False, svgns=True)
        return buf.getvalue().decode()
    except Exception:
        return None
