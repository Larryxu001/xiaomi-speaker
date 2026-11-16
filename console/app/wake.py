"""Wake replies ("我在", "在呢"...) in the chosen voice.

The firmware answers the wake word itself by playing short WAVs from
/usr/share/sound-vendor/<vendor>/wakeup_*.wav. The speaker's poller asks for
these clips whenever the voice changes and bind-mounts them over the originals
(in /tmp, so a reboot restores the factory sounds), so the first thing you
hear is already the voice you picked.
"""

from __future__ import annotations

import array
import io
import wave

import miniaudio

from . import tts

# Same file names and meaning as the firmware's own clips.
# Whole short phrases: a lone syllable ("嗯", "在") comes out drawn-out and
# sleepy from most TTS voices, unlike the sentences that follow.
CLIPS = {
    "wakeup_wozai.wav": "我在。",
    "wakeup_zai_01.wav": "我在呢。",
    "wakeup_zai_02.wav": "在的。",
    "wakeup_ei_01.wav": "你说。",
    "wakeup_ei_02.wav": "你说吧。",
}
CLIPS_VERSION = "2"  # bump to re-render clips on the speaker
RATE = 16_000
_cache: dict[tuple, bytes] = {}


def signature(settings: dict) -> str:
    """Changes whenever the wake clips would sound different."""
    voice = settings.get("tts_voice", tts.NATIVE)
    if voice == tts.NATIVE or voice not in tts.LABELS or tts.voice_problem(voice, settings):
        return tts.NATIVE
    return f"{voice}|{settings.get('tts_rate', 'normal')}|{CLIPS_VERSION}"


def _trim(samples: array.array, threshold: int = 500, pad: int = RATE // 50) -> array.array:
    loud = [i for i, v in enumerate(samples) if abs(v) > threshold]
    if not loud:
        return samples
    return samples[max(0, loud[0] - pad): min(len(samples), loud[-1] + pad)]


async def clip(name: str, settings: dict) -> bytes | None:
    """16 kHz mono 16-bit WAV for one wake clip, or None for the native voice."""
    sig = signature(settings)
    if sig == tts.NATIVE or name not in CLIPS:
        return None
    key = (sig, name)
    if key in _cache:
        return _cache[key]
    audio = await tts.synthesize(CLIPS[name], settings["tts_voice"], settings)
    decoded = miniaudio.decode(audio, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=RATE)
    samples = _trim(decoded.samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    _cache[key] = buf.getvalue()
    return _cache[key]
