"""Made-up data for tests: no real library, no Plex server, no audio files."""
import numpy as np

RNG = np.random.default_rng(1234)


def fingerprint(n=948):
    return RNG.integers(0, 2 ** 32, size=n, dtype=np.uint64).astype(np.uint32)


def noisy_copy(fp, flip_share=0.02):
    """The same audio re-encoded: a small share of fingerprint bits flip."""
    bits = RNG.random((len(fp), 32)) < flip_share
    mask = (bits * (1 << np.arange(32, dtype=np.uint64))).sum(axis=1).astype(np.uint32)
    return fp ^ mask


def track(i, title, artist="Artist", album="Album", duration=200.0, codec="mp3", bitrate=320, folder="/music/Artist/Album"):
    return {"id": i, "title": title, "artist": artist, "album": album, "duration": duration, "codec": codec,
            "bitrate": bitrate, "size": int(bitrate * duration * 125), "file": f"{folder}/{i:02d} {title}.{codec}",
            "analysed": True}


def stream(loudness=-12.0, lra=6.0, peak=0.95, channels=2.0):
    return {"loudness": loudness, "lra": lra, "peak": peak, "channels": channels}
