import base64
import inspect
import dataclasses
from json import dumps

import eyed3.core
import eyed3.plugins
import eyed3.id3.tag
import eyed3.id3.headers
import eyed3.id3.frames

from eyed3.utils.log import getLogger

log = getLogger(__name__)


class JsonTagPlugin(eyed3.plugins.LoaderPlugin):
    NAMES = ["json"]
    SUMMARY = "Outputs all tags as JSON."

    def __init__(self, arg_parser):
        super().__init__(arg_parser, cache_files=True, track_images=False)
        g = self.arg_group
        g.add_argument("-c", "--compact", action="store_true",
                       help="Output in compact form, wound new lines or indentation.")
        g.add_argument("-s", "--sort", action="store_true", help="Output JSON in sorted by key.")

    def handleFile(self, f, *args, **kwargs):
        super().handleFile(f)
        if self.audio_file and self.audio_file.info and self.audio_file.tag:
            json = audioFileToJson(self.audio_file)
            print(dumps(json, indent=2 if not self.args.compact else None,
                        sort_keys=self.args.sort))


def audioFileToJson(audio_file):
    tag = audio_file.tag

    tdict = dict(path=audio_file.path, info=dataclasses.asdict(audio_file.info))

    # Tag fields
    for name in [m for m in sorted(dir(tag)) if not m.startswith("_") and m not in _tag_exclusions]:
        member = getattr(tag, name)

        if name not in _tag_map:
            if not inspect.ismethod(member) and not inspect.isfunction(member):
                log.warning(f"Unhandled Tag member: {name}")
            continue
        elif member is None:
            continue
        elif member.__class__ is not _tag_map[name]:
            log.warning(f"Unexpected type for member {name}: {member.__class__}")
            continue

        if isinstance(member, (str, int, bool)):
            if isinstance(member, str) and name in _text_frame_fids:
                texts = _textFrameValues(tag, name)
                tdict[name] = texts if len(texts) > 1 else member
            else:
                tdict[name] = member
        elif isinstance(member, eyed3.core.Date):
            tdict[name] = str(member)
        elif isinstance(member, eyed3.id3.Genre):
            tdict[name] = member.name
        elif isinstance(member, bytes):
            tdict[name] = base64.b64encode(member).decode("ascii")
        elif isinstance(member, eyed3.id3.tag.ArtistOrigin):
            ...  # TODO
        elif isinstance(member, (eyed3.core.CountAndTotalTuple,)):
            if any(member):
                tdict[name] = member._asdict()
        elif isinstance(member, (list, tuple)):
            ...  # TODO
        elif isinstance(member, eyed3.id3.tag.AccessorBase):
            ...  # TODO
        elif isinstance(member, (eyed3.id3.tag.TagHeader, eyed3.id3.tag.ExtendedTagHeader,
                                 eyed3.id3.tag.FileInfo, eyed3.id3.frames.FrameSet)):
            ...  # TODO
        else:
            log.warning(f"Unhandled tag member {name}, type {member.__class__.__name__})")

    tdict["_eyeD3"] = eyed3.__about__.__version__
    return tdict


def _textFrameValues(tag, name):
    """Return the list of values for the text frame backing ``name``.

    ``name`` is one of the keys in ``_text_frame_fids``. Text frames are
    stored one-per-id so the first frame is used."""
    frames = tag.frame_set[_text_frame_fids[name]]
    if not frames:
        return []
    return frames[0].texts


# Tag member names backed by a single text frame. These are the fields that
# may contain multiple, null separated, values in ID3 v2.4.
_text_frame_fids = {
    "album": eyed3.id3.frames.ALBUM_FID,
    "album_artist": eyed3.id3.frames.ALBUM_ARTIST_FID,
    "artist": eyed3.id3.frames.ARTIST_FID,
    "original_artist": eyed3.id3.frames.ORIG_ARTIST_FID,
    "composer": eyed3.id3.frames.COMPOSER_FID,
    "publisher": eyed3.id3.frames.PUBLISHER_FID,
    "title": eyed3.id3.frames.TITLE_FID,
}


_tag_map = {
    'album': str,
    'album_artist': str,
    'album_type': str,
    'artist': str,
    'original_artist': str,
    'artist_origin': list,
    'artist_url': str,
    'audio_file_url': str,
    'audio_source_url': str,
    'best_release_date': eyed3.core.Date,
    'bpm': int,
    'cd_id': bytes,
    'chapters': eyed3.id3.tag.ChaptersAccessor,
    'comments': eyed3.id3.tag.CommentsAccessor,
    'commercial_url': str,
    'composer': str,
    'copyright_url': str,
    'disc_num': eyed3.core.CountAndTotalTuple,
    'encoding_date': eyed3.core.Date,
    'extended_header': eyed3.id3.headers.ExtendedTagHeader,
    'file_info': eyed3.id3.tag.FileInfo,
    'frame_set': eyed3.id3.frames.FrameSet,
    'genre': eyed3.id3.Genre,
    'header': eyed3.id3.headers.TagHeader,
    'images': eyed3.id3.tag.ImagesAccessor,
    'internet_radio_url': str,
    'lyrics': eyed3.id3.tag.LyricsAccessor,
    'non_std_genre': eyed3.id3.Genre,
    'objects': eyed3.id3.tag.ObjectsAccessor,
    'original_release_date': eyed3.core.Date,
    'payment_url': str,
    'play_count': int,
    'popularities': eyed3.id3.tag.PopularitiesAccessor,
    'privates': eyed3.id3.tag.PrivatesAccessor,
    'publisher': str,
    'publisher_url': str,
    'recording_date': eyed3.core.Date,
    'release_date': eyed3.core.Date,
    'table_of_contents': eyed3.id3.tag.TocAccessor,
    'tagging_date': eyed3.core.Date,
    'terms_of_use': str,
    'title': str,
    'track_num': eyed3.core.CountAndTotalTuple,
    'unique_file_ids': eyed3.id3.tag.UniqueFileIdAccessor,
    'user_text_frames': eyed3.id3.tag.UserTextsAccessor,
    'user_url_frames': eyed3.id3.tag.UserUrlsAccessor,
    'version': tuple,
}

_tag_exclusions = {
    "read_only": bool,
}
