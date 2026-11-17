"""Byte exact, hand built MP3 fixtures for the audio info computation.

The samples are assembled from raw MPEG frame headers (and Xing/Info, VBRI,
or ID3 tag bytes where relevant) so that the numbers reported by
:class:`eyed3.mp3.Mp3AudioInfo` can be checked against values known at build
time. No encoder or external data files are required.
"""
import struct

import pytest

import eyed3
from eyed3.mp3.headers import (BIT_RATE_TABLE, SAMPLES_PER_FRAME_TABLE,
                               Mp3Header, timePerFrame, _mp3VersionKey)


# MPEG version -> (header version bits, sample rate table column)
VERSIONS = {
    1.0: (0x3, 0),
    2.0: (0x2, 1),
    2.5: (0x0, 2),
}

# (version, layer) -> bit rate table column
BIT_RATE_COL = {
    (1.0, 1): 0, (1.0, 2): 1, (1.0, 3): 2,
    (2.0, 1): 3, (2.0, 2): 4, (2.0, 3): 4,
    (2.5, 1): 3, (2.5, 2): 4, (2.5, 3): 4,
}

SAMPLE_RATES = {
    1.0: (44100, 48000, 32000),
    2.0: (22050, 24000, 16000),
    2.5: (11025, 12000, 8000),
}


def bitrate_index(version, layer, bit_rate):
    """Return the 4 bit header index for ``bit_rate`` (kb/s)."""
    col = BIT_RATE_COL[(version, layer)]
    for idx, row in enumerate(BIT_RATE_TABLE):
        if row[col] == bit_rate:
            return idx
    raise ValueError("bit rate %d not available for %s layer %d" %
                     (bit_rate, version, layer))


def frame_header(version, layer, bit_rate, sample_freq, padding=0,
                 mode=0x0):
    """Build a 4 byte MPEG audio frame header."""
    version_bits = VERSIONS[version][0]
    sample_idx = SAMPLE_RATES[version].index(sample_freq)
    br_idx = bitrate_index(version, layer, bit_rate)
    layer_bits = 4 - layer  # layer I -> 3, II -> 2, III -> 1

    header = (0xffe00000 |
              (version_bits << 19) |
              (layer_bits << 17) |
              (1 << 16) |          # no CRC
              (br_idx << 12) |
              (sample_idx << 10) |
              (padding << 9) |
              (mode << 6))
    return struct.pack(">I", header)


def frame_length(version, layer, bit_rate, sample_freq, padding=0):
    return Mp3Header(struct.unpack(
        ">I", frame_header(version, layer, bit_rate, sample_freq,
                           padding))[0]).frame_length


def make_frame(version, layer, bit_rate, sample_freq, padding=0,
               payload=0x00):
    """A full sized frame: valid header followed by ``payload`` bytes."""
    header = frame_header(version, layer, bit_rate, sample_freq, padding)
    length = frame_length(version, layer, bit_rate, sample_freq, padding)
    return header + bytes([payload]) * (length - 4)


def xing_frame(version, layer, bit_rate, sample_freq, tag, num_frames,
               num_bytes, mode=0x0):
    """Build a first frame containing a Xing (VBR) or Info (CBR) header."""
    assert tag in (b"Xing", b"Info")
    header = frame_header(version, layer, bit_rate, sample_freq, mode=mode)
    length = frame_length(version, layer, bit_rate, sample_freq)
    frame = bytearray(length)
    frame[0:4] = header

    version_bit = (header[1] >> 3) & 0x1
    mode_bits = (header[3] >> 6) & 0x3
    if version_bit:
        pos = 32 + 4 if mode_bits != 3 else 17 + 4
    else:
        pos = 17 + 4 if mode_bits != 3 else 9 + 4

    frame[pos:pos + 4] = tag
    pos += 4
    # FRAMES_FLAG | BYTES_FLAG
    frame[pos:pos + 4] = struct.pack(">I", 0x3)
    pos += 4
    frame[pos:pos + 4] = struct.pack(">I", num_frames)
    pos += 4
    frame[pos:pos + 4] = struct.pack(">I", num_bytes)
    return bytes(frame)


def vbri_frame(version, layer, bit_rate, sample_freq, num_frames,
               num_bytes):
    """Build a first frame containing a VBRI header (always VBR)."""
    header = frame_header(version, layer, bit_rate, sample_freq)
    length = frame_length(version, layer, bit_rate, sample_freq)
    frame = bytearray(length)
    frame[0:4] = header

    offset = 36
    frame[offset:offset + 4] = b"VBRI"
    offset += 4
    frame[offset:offset + 2] = struct.pack(">H", 1)       # version
    offset += 2
    frame[offset:offset + 2] = struct.pack(">H", 0)       # delay
    offset += 2
    frame[offset:offset + 2] = struct.pack(">H", 0)       # quality
    offset += 2
    frame[offset:offset + 4] = struct.pack(">I", num_bytes)
    offset += 4
    frame[offset:offset + 4] = struct.pack(">I", num_frames)
    return bytes(frame)


def id3v2_tag(size):
    """A minimal ID3v2.3 tag of ``size`` total bytes (10 byte header)."""
    body = size - 10
    header = b"ID3" + bytes([0x03, 0x00, 0x00])
    # 4 syncsafe bytes
    header += bytes([(body >> 21) & 0x7f, (body >> 14) & 0x7f,
                     (body >> 7) & 0x7f, body & 0x7f])
    return header + b"\x00" * body


ID3V1_TAG = b"TAG" + b"\x00" * 125


def write_mp3(tmp_path, name, chunks):
    pth = tmp_path / name
    with open(pth, "wb") as fp:
        for chunk in chunks:
            fp.write(chunk)
    return pth


def expected_time(version, layer, sample_freq, num_frames):
    samples = SAMPLES_PER_FRAME_TABLE[_mp3VersionKey(version)][layer]
    return num_frames * samples / sample_freq


@pytest.fixture
def load():
    def _load(pth):
        audio_file = eyed3.load(str(pth))
        assert audio_file is not None
        assert audio_file.info is not None
        return audio_file
    return _load


def test_frame_length_and_samples_per_frame():
    # MPEG-2/2.5 Layer III frames carry 576 samples and use the 72
    # coefficient; the old code used the MPEG-1 144 coefficient.
    cases = [
        # version, layer, bit_rate, sample_freq, expected length
        (2.0, 3, 64, 22050, 208),    # int(72 * 64000 / 22050)
        (2.0, 3, 32, 22050, 104),
        (2.5, 3, 32, 11025, 208),   # int(72 * 32000 / 11025)
        (2.5, 3, 16, 8000, 144),
        (1.0, 3, 128, 44100, 417),   # 144 * 128000 / 44100
        (1.0, 2, 192, 48000, 576),   # Layer II keeps the 144 coefficient
        (2.0, 2, 64, 24000, 384),
        (1.0, 1, 384, 32000, 576),   # Layer I: 4 * (12 * br / sf)
    ]
    for version, layer, br, sf, expected in cases:
        h = Mp3Header(struct.unpack(
            ">I", frame_header(version, layer, br, sf))[0])
        assert h.frame_length == expected
        tpf = timePerFrame(h)
        samples = SAMPLES_PER_FRAME_TABLE[_mp3VersionKey(version)][layer]
        assert tpf == samples / sf


@pytest.mark.parametrize("version,sample_freq",
                         [(1.0, 44100), (2.0, 22050), (2.5, 11025)])
def test_xing_vbr_header(tmp_path, load, version, sample_freq):
    layer = 3
    first_br, other_br = 128, 64
    num_data_frames = 5
    first = xing_frame(version, layer, first_br, sample_freq, b"Xing",
                       num_frames=num_data_frames + 1,
                       num_bytes=12345)
    rest = b"".join(make_frame(version, layer, other_br, sample_freq)
                    for _ in range(num_data_frames))
    pth = write_mp3(tmp_path, "xing-%s.mp3" % version, [first, rest])

    audio_file = load(pth)
    info = audio_file.info
    assert info.xing_header is not None and info.xing_header.vbr
    assert info.vbri_header is None
    assert info.bit_rate[0] is True
    expected_secs = expected_time(version, layer, sample_freq,
                                  num_data_frames + 1)
    assert info.time_secs == int(expected_secs * 100) / 100
    assert info.bit_rate[1] == int((12345 * 8) / (expected_secs * 1000))
    assert info.bit_rate_str.startswith("~")
    assert info.mp3_header.version == version
    assert info.mp3_header.sample_freq == sample_freq


@pytest.mark.parametrize("version,sample_freq",
                         [(1.0, 44100), (2.0, 22050), (2.5, 11025)])
def test_info_header_is_cbr(tmp_path, load, version, sample_freq):
    layer = 3
    bit_rate = 128
    num_frames = 6
    audio_bytes = sum(frame_length(version, layer, bit_rate, sample_freq)
                      for _ in range(num_frames))
    first = xing_frame(version, layer, bit_rate, sample_freq, b"Info",
                       num_frames=num_frames, num_bytes=audio_bytes)
    rest = b"".join(make_frame(version, layer, bit_rate, sample_freq)
                    for _ in range(num_frames - 1))
    pth = write_mp3(tmp_path, "info-%s.mp3" % version, [first, rest])

    info = load(pth).info
    assert info.xing_header is not None and not info.xing_header.vbr
    assert info.bit_rate[0] is False
    expected_secs = expected_time(version, layer, sample_freq, num_frames)
    assert info.time_secs == int(expected_secs * 100) / 100
    assert info.bit_rate[1] == int((audio_bytes * 8) /
                                   (expected_secs * 1000))
    assert info.bit_rate_str == "%d kb/s" % info.bit_rate[1]


@pytest.mark.parametrize("version,sample_freq",
                         [(1.0, 44100), (2.0, 22050), (2.5, 11025)])
def test_vbri_header_is_vbr(tmp_path, load, version, sample_freq):
    layer = 3
    first_br, other_br = 128, 64
    num_data_frames = 4
    num_frames = num_data_frames + 1
    num_bytes = 9876
    first = vbri_frame(version, layer, first_br, sample_freq,
                       num_frames=num_frames, num_bytes=num_bytes)
    rest = b"".join(make_frame(version, layer, other_br, sample_freq)
                    for _ in range(num_data_frames))
    pth = write_mp3(tmp_path, "vbri-%s.mp3" % version, [first, rest])

    info = load(pth).info
    assert info.vbri_header is not None
    assert info.xing_header is None
    assert info.bit_rate[0] is True
    expected_secs = expected_time(version, layer, sample_freq, num_frames)
    assert info.time_secs == int(expected_secs * 100) / 100
    assert info.bit_rate[1] == int((num_bytes * 8) /
                                   (expected_secs * 1000))


def test_headless_vbr_walks_frames(tmp_path, load):
    version, layer, sf = 1.0, 3, 44100
    bit_rates = [128, 96, 112, 64, 128]
    frames = [make_frame(version, layer, br, sf) for br in bit_rates]
    pth = write_mp3(tmp_path, "headless-vbr.mp3", frames)

    info = load(pth).info
    assert info.xing_header is None and info.vbri_header is None
    assert info.bit_rate[0] is True
    audio_bytes = sum(frame_length(version, layer, br, sf)
                      for br in bit_rates)
    expected_secs = expected_time(version, layer, sf, len(bit_rates))
    assert info.time_secs == int(expected_secs * 100) / 100
    assert info.bit_rate[1] == int((audio_bytes * 8) /
                                   (expected_secs * 1000))


def test_headless_cbr_uses_first_frame_bitrate(tmp_path, load):
    version, layer, sf, br = 2.5, 3, 11025, 32
    count = 7
    frames = [make_frame(version, layer, br, sf) for _ in range(count)]
    pth = write_mp3(tmp_path, "headless-cbr.mp3", frames)

    info = load(pth).info
    assert info.bit_rate == (False, br)
    expected_secs = expected_time(version, layer, sf, count)
    assert info.time_secs == int(expected_secs * 100) / 100


def test_headless_cbr_with_padding(tmp_path, load):
    # Padding changes frame lengths but not the bit rate, so the file is
    # still CBR and must not be misclassified.
    version, layer, sf, br = 1.0, 3, 44100, 128
    frames = [make_frame(version, layer, br, sf, padding=p)
              for p in (0, 1, 0, 1)]
    pth = write_mp3(tmp_path, "padded-cbr.mp3", frames)

    info = load(pth).info
    assert info.bit_rate == (False, br)
    assert info.time_secs == int(
        expected_time(version, layer, sf, len(frames)) * 100) / 100


def test_v2_and_v1_tags_excluded_from_audio(tmp_path, load):
    version, layer, sf, br = (1.0, 3, 44100, 128)
    count = 5
    frames = [make_frame(version, layer, br, sf) for _ in range(count)]
    audio_bytes = sum(len(f) for f in frames)
    v2 = id3v2_tag(42)     # 10 byte header + 32 byte body
    pth = write_mp3(tmp_path, "dual-tags.mp3",
                    [v2, b"".join(frames), ID3V1_TAG])

    audio_file = load(pth)
    assert audio_file.tag is not None and audio_file.tag.isV2()
    info = audio_file.info
    # Tag bytes must not inflate the duration or average bit rate.
    expected_secs = expected_time(version, layer, sf, count)
    assert info.time_secs == int(expected_secs * 100) / 100
    assert info.bit_rate == (False, br)
    assert info.size_bytes == len(v2) + audio_bytes + len(ID3V1_TAG)


def test_v1_only_tag_excluded(tmp_path, load):
    version, layer, sf, br = 2.0, 3, 22050, 64
    count = 4
    frames = b"".join(make_frame(version, layer, br, sf)
                      for _ in range(count))
    pth = write_mp3(tmp_path, "v1-only.mp3", [frames, ID3V1_TAG])

    audio_file = load(pth)
    assert audio_file.tag is not None and audio_file.tag.isV1()
    info = audio_file.info
    expected_secs = expected_time(version, layer, sf, count)
    assert info.time_secs == int(expected_secs * 100) / 100
    assert info.bit_rate == (False, br)


def test_cli_audio_info_line(tmp_path):
    from eyed3 import main
    from tests import RedirectStdStreams

    version, layer, sf, br = 2.5, 3, 11025, 32
    frames = b"".join(make_frame(version, layer, br, sf) for _ in range(3))
    pth = write_mp3(tmp_path, "cli.mp3", [frames])

    args, _, config = main.parseCommandLine([str(pth)])
    with RedirectStdStreams() as out:
        assert main.main(args, config) == 0
    output = out.stdout.getvalue()
    assert "MPEG2.5" in output
    assert "MPEG2," not in output
    assert "Layer III" in output
    assert "%d kb/s" % br in output


def test_stats_vbr_counter(tmp_path):
    from eyed3 import main
    from eyed3.plugins.stats import BitrateCounter
    from tests import RedirectStdStreams

    cbr = write_mp3(
        tmp_path, "stats-cbr.mp3",
        [b"".join(make_frame(1.0, 3, 128, 44100) for _ in range(3))])
    vbr = write_mp3(
        tmp_path, "stats-vbr.mp3",
        [b"".join(make_frame(1.0, 3, br, 44100)
                  for br in (128, 96, 64))])

    args, _, config = main.parseCommandLine(
        ["--plugin", "stats", str(cbr), str(vbr)])
    with RedirectStdStreams() as out:
        assert main.main(args, config) == 0
    output = out.stdout.getvalue()
    # The BitrateCounter report prints the cbr/vbr tally lines.
    tally = {}
    for line in output.splitlines():
        parts = line.split(":")
        key = parts[0].strip()
        if key in ("cbr", "vbr"):
            tally[key] = int(parts[1].strip().split()[0])
    assert tally == {"cbr": 1, "vbr": 1}

    # Direct counter check, independent of report formatting.
    counter = BitrateCounter()
    counter.compute(eyed3.load(str(cbr)))
    counter.compute(eyed3.load(str(vbr)))
    assert counter["cbr"] == 1 and counter["vbr"] == 1
