import os
import re
import stat

from .. import Error
from .. import id3
from .. import core

from ..utils.log import getLogger
log = getLogger(__name__)


class Mp3Exception(Error):
    """Used to signal mp3-related errors."""
    pass


NAME = "mpeg"
# Mime-types that are recognized at MP3
MIME_TYPES = ["audio/mpeg", "audio/mp3", "audio/x-mp3", "audio/x-mpeg",
              "audio/mpeg3", "audio/x-mpeg3", "audio/mpg", "audio/x-mpg",
              "audio/x-mpegaudio", "audio/mpegapplication/x-tar",
             ]

# Mime-types that have been seen to contain mp3 data.
OTHER_MIME_TYPES = ['application/octet-stream',  # ???
                    'audio/x-hx-aac-adts',  # ???
                    'audio/x-wav',  # RIFF wrapped mp3s
                   ]

# Valid file extensions.
EXTENSIONS = [".mp3"]


class Mp3AudioInfo(core.AudioInfo):
    #: Size in bytes of an ID3 v1.x tag, always found at the end of a file.
    V1_TAG_SIZE = 128
    #: The 3 byte identifier that begins an ID3 v1.x tag.
    V1_TAG_ID = b"TAG"

    def __init__(self, file_obj, start_offset, tag):
        from . import headers
        from .headers import timePerFrame

        log.debug("mp3 header search starting @ %x" % start_offset)

        self.mp3_header = None
        self.xing_header = None
        self.vbri_header = None
        # If not ``None``, the Lame header.
        # See :class:`eyed3.mp3.headers.LameHeader`
        self.lame_tag = None
        # 2-tuple, (vrb?:boolean, bitrate:int)
        self.bit_rate = (None, None)

        header_pos = 0
        while self.mp3_header is None:
            # Find first mp3 header
            (header_pos,
             header_int,
             header_bytes) = headers.findHeader(file_obj, start_offset)
            if not header_int:
                try:
                    fname = file_obj.name
                except AttributeError:
                    fname = 'unknown'
                raise headers.Mp3Exception(
                    "Unable to find a valid mp3 frame in '%s'" % fname)

            try:
                self.mp3_header = headers.Mp3Header(header_int)
                log.debug("mp3 header %x found at position: 0x%x" %
                          (header_int, header_pos))
            except headers.Mp3Exception as ex:
                log.debug("Invalid mp3 header: %s" % str(ex))
                # keep looking...
                start_offset += 4

        file_obj.seek(header_pos)
        mp3_frame = file_obj.read(self.mp3_header.frame_length)
        if re.compile(b'Xing|Info').search(mp3_frame):
            # Check for Xing/Info header information.
            self.xing_header = headers.XingHeader()
            if not self.xing_header.decode(mp3_frame):
                log.debug("Ignoring corrupt Xing header")
                self.xing_header = None
        elif mp3_frame.find(b'VBRI') >= 0:
            # Check for VBRI header information.
            self.vbri_header = headers.VbriHeader()
            if not self.vbri_header.decode(mp3_frame):
                log.debug("Ignoring corrupt VBRI header")
                self.vbri_header = None

        # Check for LAME Tag
        self.lame_tag = headers.LameHeader(mp3_frame)

        # Set file size
        size_bytes = os.stat(file_obj.name)[stat.ST_SIZE]

        # The byte range that holds MPEG audio; ID3 v2 prefixes and ID3 v1
        # trailers are not audio.
        audio_start = header_pos
        audio_end = self._computeAudioEnd(file_obj, size_bytes, tag)

        tpf = timePerFrame(self.mp3_header, False)

        # Compute track play time and average bit rate. When a Xing/Info or
        # VBRI header is present its frame and byte counts are authoritative.
        if self.xing_header and self.xing_header.numFrames:
            time_secs = tpf * self.xing_header.numFrames
        elif self.vbri_header and self.vbri_header.num_frames:
            time_secs = tpf * self.vbri_header.num_frames
        else:
            time_secs = None

        if self.xing_header and self.xing_header.numFrames and \
                self.xing_header.numBytes:
            # An "Info" header marks a CBR encode, "Xing" a VBR encode.
            vbr = self.xing_header.vbr
            br = int((self.xing_header.numBytes * 8) /
                     (time_secs * 1000)) if time_secs else 0
        elif self.vbri_header and self.vbri_header.num_frames and \
                self.vbri_header.num_bytes:
            # VBRI headers are only written by VBR encoders.
            vbr = True
            br = int((self.vbri_header.num_bytes * 8) /
                     (time_secs * 1000)) if time_secs else 0
        else:
            # No VBR header. Walk the frames to distinguish VBR from CBR and
            # to get exact counts; the reads stay frame sized.
            vbr, frame_count, audio_byte_count, constant_bit_rate = \
                self._walkFrames(file_obj, headers, audio_start, audio_end)

            if time_secs is None:
                time_secs = tpf * frame_count

            if vbr and time_secs:
                br = int((audio_byte_count * 8) / (time_secs * 1000))
            else:
                # Pure CBR: the first frame's bit rate applies to every
                # frame, no scan needed.
                br = constant_bit_rate

        if time_secs is None:
            # No frames could be read; fall back to the size based estimate.
            length = max(audio_end - audio_start, 0)
            time_secs = (length / self.mp3_header.frame_length) * tpf

        self.bit_rate = (vbr, br)

        self.sample_freq = self.mp3_header.sample_freq
        self.mode = self.mp3_header.mode

        super().__init__(time_secs, size_bytes)

    def _computeAudioEnd(self, file_obj, size_bytes, tag):
        """Return the offset immediately following the last MPEG audio byte.
        ID3 v2 headers prefix the audio (already excluded via the start
        offset); ID3 v1 tags are 128 byte trailers."""
        audio_end = size_bytes
        if tag and tag.isV1():
            audio_end -= self.V1_TAG_SIZE
        elif size_bytes >= self.V1_TAG_SIZE:
            # A v1 tag may be present even when only a v2 tag was parsed
            # (e.g. a file carrying both v2 and v1 tags).
            file_obj.seek(-self.V1_TAG_SIZE, 2)
            if file_obj.read(3) == self.V1_TAG_ID:
                audio_end -= self.V1_TAG_SIZE
        return audio_end

    def _walkFrames(self, file_obj, headers, audio_start, audio_end):
        """Scan the audio frames, reading one 4 byte header at a time.

        Returns a 4-tuple of (vbr?, frame count, audio byte count, bit rate
        of the first frame). The bit rate varies between frames in VBR
        files; a CBR file's frames all share the first frame's bit rate.
        """
        frame_count = 0
        audio_byte_count = 0
        bit_rates = set()

        for frame_offset, header in headers.walkMp3Frames(
                file_obj, self.mp3_header, audio_start, audio_end):
            frame_count += 1
            bit_rates.add(header.bit_rate)
            if frame_count == 1:
                first_bit_rate = header.bit_rate

            next_offset = frame_offset + header.frame_length
            if next_offset > audio_end:
                # Count the bytes of the truncated final frame.
                audio_byte_count += audio_end - frame_offset
            else:
                audio_byte_count += header.frame_length

        if frame_count == 0:
            # Behave like the size based estimate used below.
            return (False, 0, max(audio_end - audio_start, 0),
                    self.mp3_header.bit_rate)

        vbr = len(bit_rates) > 1
        return vbr, frame_count, audio_byte_count, first_bit_rate

    ##
    # Helper to get the bitrate as a string. The prefix '~' is used to denote
    # variable bit rates.
    @property
    def bit_rate_str(self):
        (vbr, bit_rate) = self.bit_rate
        return f"{'~' if vbr else ''}{bit_rate} kb/s"


class Mp3AudioFile(core.AudioFile):
    """Audio file container for mp3 files."""

    def __init__(self, path, version=id3.ID3_ANY_VERSION):
        self._tag_version = version

        super().__init__(path)
        assert self.type == core.AUDIO_MP3

    def _read(self):
        with open(self.path, "rb") as file_obj:
            self._tag = id3.Tag()
            tag_found = self._tag.parse(file_obj, self._tag_version)

            # Compute offset for starting mp3 data search
            if tag_found and self._tag.isV1():
                mp3_offset = 0
            elif tag_found and self._tag.isV2():
                mp3_offset = self._tag.header.SIZE + self._tag.header.tag_size
            else:
                mp3_offset = 0
                self._tag = None

            try:
                self._info = Mp3AudioInfo(file_obj, mp3_offset, self._tag)
            except Mp3Exception as ex:
                # Only logging a warning here since we can still operate on
                # the tag.
                log.warning(ex)
                self._info = None

            self.type = core.AUDIO_MP3

    def initTag(self, version=id3.ID3_DEFAULT_VERSION):
        """Add a id3.Tag to the file (removing any existing tag if one exists)."""
        self.tag = id3.Tag()
        self.tag.version = version
        self.tag.file_info = id3.FileInfo(self.path)
        return self.tag

    @core.AudioFile.tag.setter
    def tag(self, t):
        if t:
            t.file_info = id3.FileInfo(self.path)
            if self._tag and self._tag.file_info:
                t.file_info.tag_size = self._tag.file_info.tag_size
                t.file_info.tag_padding_size = \
                    self._tag.file_info.tag_padding_size
        self._tag = t
