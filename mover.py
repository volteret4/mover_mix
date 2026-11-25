"""Copia canciones a la carpeta Mix (o las reordena dentro de ella) sin interfaz.

La canción llega como id de Navidrome, como ruta o como artista + título; su ruta real
sale de la base de Navidrome (raíz de la biblioteca + media_file.path). Si Mix se monta
en la misma ruta que en el equipo donde se usan los .m3u, valen allí tal cual.
"""
import collections
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse

import mutagen
import requests

MIX = os.environ.get("MIX_PATH", "/mix")
NAVIDROME_DB = os.environ.get("NAVIDROME_DB", "/nd/navidrome.db")
MFUZZ_DB = os.environ.get("MFUZZ_DB", "/mfuzz/musica_local.sqlite")
# Raíz de cada biblioteca de Navidrome (ruta dentro de su contenedor -> ruta aquí)
LIBRARY_ROOTS = json.loads(os.environ.get("LIBRARY_ROOTS", json.dumps({
    "/music": "/music",
    "/mix": MIX,
})))
# Bibliotecas de Navidrome que son la propia Mix (al buscar por etiquetas se prefiere el resto)
MIX_LIBRARIES = [k for k, v in LIBRARY_ROOTS.items() if os.path.normpath(v) == os.path.normpath(MIX)] or ["/mix"]
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(os.environ.get("HOME", "/data"), ".cache", "mover_mix")

AUDIO_EXTS = ('.mp3', '.flac', '.m4a', '.wav', '.aiff', '.aif', '.ogg', '.opus')
LOSSLESS_EXTS = ('.flac', '.wav', '.ape', '.wv', '.tta', '.aiff', '.aif')
PLAYLIST_EXTS = ('.mp3', '.flac', '.ogg', '.wav', '.m4a')

# Igual que en el script de escritorio
AGREGADAS_ACTIVAS = False
PLAYLISTS_AGREGADAS = {
    "ELECTRONICA": ["Techno", "6_house", "idm", "N_DnB", "breaks", "dance", "Electro",
                    "uk garage", "big beat", "keet", "udub",
                    "Pop/pop_synthpop", "Pop/pop_dance"],
}


class MoverError(Exception):
    pass


# ── utilidades ─────────────────────────────────────────────────────────────

def copy_sin_permisos(src, dst):
    """Contenido y fechas, sin permisos (como en el escritorio)."""
    shutil.copyfile(src, dst)
    st = os.stat(src)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    return dst


def nombre_mix(artist, title, album="", date="", label=""):
    """«Artista - Título (año álbum) [sello]» sin extensión; None si falta artista o título."""
    artist, title = (artist or "").strip(), (title or "").strip()
    if not artist or not title:
        return None
    name = f"{artist} - {title}"
    date_album = " ".join(p for p in ((date or "").strip(), (album or "").strip()) if p)
    if date_album:
        name += f" ({date_album})"
    label = (label or "").strip()
    if label and label.lower() not in ("[no label]", "no label", "not on label"):
        name += f" [{label}]"
    name = re.sub(r'[/\\?%*:|"<>]', '-', name)
    return re.sub(r'\s+', ' ', name).strip()


def safe_name(s):
    return re.sub(r'[/\\?%*:|"<>]', '-', s)


def artist_names(artist):
    """'Joe Goddard (feat. Alexis Taylor)' -> {'joe goddard', 'alexis taylor'}"""
    artist = re.sub(r'[()\[\]]', ' ', artist or '')
    parts = re.split(r'\s*(?:,|;|/|&|\bfeat\.?|\bft\.?|\bfeaturing\b|\bvs\.?|\bx\b)\s*', artist, flags=re.I)
    return {p.strip().casefold() for p in parts if p.strip()}


def read_tags(path):
    """Etiquetas básicas con mutagen (easy)."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        audio = None
    tags = (audio.tags if audio else None) or {}

    def first(*keys):
        for k in keys:
            try:
                v = tags.get(k)
            except Exception:
                v = None
            if v:
                return str(v[0] if isinstance(v, list) else v).strip()
        return ""
    length = getattr(getattr(audio, 'info', None), 'length', None)
    return {
        'artist': first('artist', 'albumartist'),
        'title': first('title'),
        'album': first('album'),
        'date': first('date', 'originaldate'),
        'label': first('label', 'organization', 'publisher'),
        'tracknumber': first('tracknumber'),
        'length': length,
    }


def read_comment(path):
    """Comentario del archivo (sustituye a «tagutil print» del escritorio)."""
    try:
        audio = mutagen.File(path)
    except Exception:
        return ""
    if not audio or not audio.tags:
        return ""
    tags = audio.tags
    try:
        if hasattr(tags, 'getall'):  # ID3
            frames = tags.getall('COMM')
            return str(frames[0].text[0]) if frames and frames[0].text else ""
        for key in ('comment', 'COMMENT', 'description', '\xa9cmt'):
            v = tags.get(key)
            if v:
                return str(v[0] if isinstance(v, list) else v)
    except Exception:
        pass
    return ""


def ffprobe_tags(path):
    r = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', path],
                       capture_output=True, text=True, check=True)
    return {k.lower(): v for k, v in json.loads(r.stdout).get('format', {}).get('tags', {}).items()}


# ── Mover ──────────────────────────────────────────────────────────────────

class Mover:
    def __init__(self, log=print):
        self.log = log

    # --- canción ---

    def song_path(self, song_id):
        """Ruta real de una canción de Navidrome."""
        con = sqlite3.connect(f"file:{NAVIDROME_DB}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute("SELECT l.path, m.path FROM media_file m JOIN library l ON l.id = m.library_id "
                              "WHERE m.id = ?", (song_id,)).fetchone()
        finally:
            con.close()
        if not row:
            raise MoverError(f"Navidrome no conoce la canción {song_id}")
        lib_root, rel = row
        root = LIBRARY_ROOTS.get(lib_root.rstrip('/'))
        if not root:
            raise MoverError(f"Biblioteca de Navidrome sin montar aquí: {lib_root}")
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            raise MoverError(f"No encuentro el archivo: {path}")
        return path

    def find_by_tags(self, artist, title, album=""):
        """Ruta real buscando en Navidrome por artista y título (reproductores que solo dan
        metadatos, como Supersonic). Prefiere la biblioteca a Mix: así se copia, nunca se mueve."""
        con = sqlite3.connect(f"file:{NAVIDROME_DB}?mode=ro", uri=True, timeout=10)
        marks = ",".join("?" * len(MIX_LIBRARIES))
        try:
            row = con.execute(f"""
                SELECT m.id FROM media_file m JOIN library l ON l.id = m.library_id
                WHERE lower(m.title) = lower(?) AND (lower(m.artist) = lower(?) OR lower(m.album_artist) = lower(?))
                ORDER BY lower(m.album) = lower(?) DESC, rtrim(l.path, '/') IN ({marks}) ASC LIMIT 1
            """, (title, artist, artist, album or "", *[p.rstrip('/') for p in MIX_LIBRARIES])).fetchone()
        finally:
            con.close()
        if not row:
            raise MoverError(f"Navidrome no tiene «{artist} - {title}»")
        return self.song_path(row[0])

    def resolve(self, song_id=None, path=None, artist=None, title=None, album=None):
        """Ruta real a partir del id de Navidrome o de una ruta (la del PC vale tal cual:
        las bibliotecas y Mix están montadas aquí en las mismas rutas)."""
        if song_id:
            return self.song_path(song_id)
        if not path and artist and title:
            return self.find_by_tags(artist, title, album)
        if not path:
            raise MoverError("Falta la canción (id o ruta)")
        real = os.path.realpath(path)
        roots = [os.path.realpath(r) for r in LIBRARY_ROOTS.values()]
        if not any(real.startswith(r + os.sep) for r in roots):
            raise MoverError(f"Ruta fuera de la biblioteca y de Mix: {path}")
        if not os.path.isfile(real):
            raise MoverError(f"No encuentro el archivo: {path}")
        return real

    def track_info(self, path):
        t = read_tags(path)
        artist, title = t['artist'], t['title']
        filename = os.path.basename(path)
        return {
            'path': path,
            'filename': filename,
            'song_name': f"{artist} - {title}" if artist and title else (title or os.path.splitext(filename)[0]),
            'artist': artist,
            'title': title,
            'album': t['album'],
            'date': t['date'],
            'label': t['label'],
            'tracknumber': t['tracknumber'],
            'length': t['length'],
            'folder': os.path.basename(os.path.dirname(path)),
        }

    def in_mix(self, path):
        return os.path.normpath(path).lower().startswith(os.path.normpath(MIX).lower() + os.sep)

    # --- base de datos de música (music-fuzzy) ---

    def _mfuzz(self):
        if not os.path.exists(MFUZZ_DB):
            return None
        con = sqlite3.connect(f"file:{MFUZZ_DB}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _album_artist_name(artist):
        return (artist or '').split('feat.')[0].split('with')[0].split('&')[0].strip()

    def find_album_row(self, track_info):
        """Fila de albums del disco: por ruta del archivo y, si no, por artista + álbum."""
        try:
            con = self._mfuzz()
            if not con:
                return None
            columns = {r[1] for r in con.execute("PRAGMA table_info(albums)")}
            candidates = []
            song = con.execute("SELECT album, artist, album_artist FROM songs WHERE file_path = ?",
                               (track_info.get('path') or '',)).fetchone()
            if song:
                candidates = [(self._album_artist_name(song['album_artist']), song['album']),
                              (self._album_artist_name(song['artist']), song['album'])]
            if track_info.get('album'):
                candidates.append((self._album_artist_name(track_info.get('artist', '')), track_info['album']))
            row = None
            for artist, album in candidates:
                if not artist or not album:
                    continue
                row = con.execute("""
                    SELECT al.* FROM albums al JOIN artists ar ON ar.id = al.artist_id
                    WHERE lower(ar.name) = lower(?) AND lower(al.name) = lower(?)
                    ORDER BY al.origen = 'local' DESC LIMIT 1
                """, (artist, album)).fetchone()
                if row:
                    break
            con.close()
            return {k: row[k] for k in columns} if row else None
        except sqlite3.Error as e:
            self.log(f"Error buscando el álbum en la db: {e}")
            return None

    @staticmethod
    def album_genres(album_row):
        if not album_row:
            return []
        discogs = ", ".join(x for x in (album_row.get('discogs_genres'), album_row.get('discogs_styles')) if x)
        sources = [("Etiquetas", album_row.get('genre')), ("Discogs", discogs),
                   ("MusicBrainz", album_row.get('mb_genres')), ("Last.fm", album_row.get('lastfm_tags'))]
        return [(n, v) for n, v in sources if v and v.strip().lower() not in ('unknown', 'none', 'other')]

    CREDIT_GROUPS = [
        ("Producción", ("produc",), ("co-produc", "executive")),
        ("Mezcla", ("mixed by", "mix"), ("remix",)),
        ("Masterización", ("master",), ()),
        ("Ingeniería", ("engineer", "recorded"), ("master", "mix")),
        ("Composición", ("written", "composed", "music by", "lyrics"), ()),
        ("Arreglos", ("arranged",), ()),
    ]

    @classmethod
    def album_credits(cls, album_row):
        if not album_row:
            return []
        try:
            credits = json.loads(album_row.get('credits') or '{}')
        except ValueError:
            credits = {}
        if not isinstance(credits, dict):
            credits = {}
        result = []
        for label, include, exclude in cls.CREDIT_GROUPS:
            names = []
            for role, people in credits.items():
                role = role.lower()
                if any(i in role for i in include) and not any(e in role for e in exclude):
                    names += [re.sub(r'\s*\(\d+\)$', '', n) for n in people if isinstance(n, str)]
            names = list(dict.fromkeys(names))
            if names:
                result.append((label, ", ".join(names[:5]) + (f" (+{len(names) - 5})" if len(names) > 5 else "")))
        if not result:
            for label, column in (("Producción", 'producers'), ("Ingeniería", 'engineers'),
                                  ("Masterización", 'mastering_engineers')):
                if album_row.get(column):
                    result.append((label, re.sub(r'\s*\(\d+\)', '', album_row[column])))
        return result

    # --- carpetas de Mix ---

    def folder_tree(self):
        """{carpeta: [subcarpetas] o None}; supergénero = carpeta con subcarpetas."""
        tree = {}
        for item in sorted(os.listdir(MIX), key=str.casefold):
            p = os.path.join(MIX, item)
            if item.startswith('.') or not os.path.isdir(p):
                continue
            children = sorted((d for d in os.listdir(p)
                               if os.path.isdir(os.path.join(p, d)) and not d.startswith('.')), key=str.casefold)
            tree[item] = children or None
        return tree

    def count_songs(self, folder, recursive=False):
        path = os.path.join(MIX, folder)
        if recursive:
            return sum(1 for _, _, files in os.walk(path) for f in files
                       if os.path.splitext(f)[1].lower() in AUDIO_EXTS)
        try:
            return sum(1 for f in os.listdir(path)
                       if os.path.splitext(f)[1].lower() in AUDIO_EXTS and os.path.isfile(os.path.join(path, f)))
        except OSError:
            return 0

    def folder_summary(self, folder):
        folder = os.path.normpath(folder)
        parts = folder.split(os.sep)
        summary = [f"{parts[-1]} ({self.count_songs(folder)})"]
        if len(parts) > 1:
            summary.append(f"{parts[0]} ({self.count_songs(parts[0], recursive=True)})")
        elif os.path.isdir(os.path.join(MIX, folder)) and any(
                os.path.isdir(os.path.join(MIX, folder, d)) for d in os.listdir(os.path.join(MIX, folder))):
            summary = [f"{parts[-1]} ({self.count_songs(folder)} sueltas, "
                       f"{self.count_songs(folder, recursive=True)} con subgéneros)"]
        return ", ".join(summary)

    def tree_with_counts(self):
        out = []
        for top, children in self.folder_tree().items():
            out.append({
                'name': top,
                'count': self.count_songs(top),
                'total': self.count_songs(top, recursive=True) if children else None,
                'children': [{'name': c, 'count': self.count_songs(os.path.join(top, c))}
                             for c in (children or [])],
            })
        return out

    # --- qué hay ya en Mix ---

    def mix_index(self):
        """[(carpeta relativa, ruta, artista, sello)] de Mix, con caché por mtime."""
        cache_file = os.path.join(CACHE_DIR, "indice.json")
        try:
            with open(cache_file) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        index, fresh = [], {}
        for root, dirs, files in os.walk(MIX):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            rel = os.path.relpath(root, MIX)
            for name in files:
                if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                    continue
                path = os.path.join(root, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                cached = cache.get(path)
                if cached and cached[0] == mtime:
                    artist, label = cached[1], cached[2]
                else:
                    t = read_tags(path)
                    artist, label = t['artist'], t['label']
                fresh[path] = [mtime, artist, label]
                index.append((rel, path, artist, label))
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache_file + ".tmp", 'w') as f:
                json.dump(fresh, f)
            os.replace(cache_file + ".tmp", cache_file)
        except OSError:
            pass
        return index

    def mix_stats(self, artist, label, exclude_path=None):
        wanted_artists = artist_names(artist)
        wanted_label = (label or '').strip().casefold()
        if wanted_label in ('[no label]', 'no label', 'not on label'):
            wanted_label = ''
        by_artist, by_label = collections.Counter(), collections.Counter()
        exclude = os.path.normcase(os.path.normpath(exclude_path)) if exclude_path else None
        for rel, path, file_artist, file_label in self.mix_index():
            if exclude and os.path.normcase(os.path.normpath(path)) == exclude:
                continue
            if wanted_artists and wanted_artists & artist_names(file_artist):
                by_artist[rel] += 1
            if wanted_label and file_label.strip().casefold() == wanted_label:
                by_label[rel] += 1
        return {'artist': by_artist.most_common(), 'label': by_label.most_common()}

    def find_duplicates(self, song_name, title=""):
        """Archivos de Mix cuyo nombre contiene «Artista - Título» o el título (sin distinguir mayúsculas)."""
        needles = [n.casefold() for n in (song_name, title) if n]
        if not needles:
            return []
        found = []
        for root, dirs, files in os.walk(MIX):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in files:
                low = f.casefold()
                if any(n in low for n in needles):
                    found.append(os.path.join(root, f))
        return sorted(found, key=str.casefold)

    # --- discos con cue ---

    def _read_cuesheet(self, path):
        try:
            r = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', path],
                               capture_output=True, text=True)
            for key, value in json.loads(r.stdout or '{}').get('format', {}).get('tags', {}).items():
                if key.lower() == 'cuesheet' and 'TRACK' in value:
                    return value, True
        except Exception as e:
            self.log(f"Error leyendo CUESHEET embebido: {e}")
        folder, base = os.path.dirname(path), os.path.basename(path)
        stem = os.path.splitext(base)[0]
        try:
            cue_files = [f for f in os.listdir(folder) if f.lower().endswith('.cue')]
        except OSError:
            return None, False
        preferred = [f for f in cue_files if os.path.splitext(f)[0] in (stem, base)]
        for cue_name in preferred + [f for f in cue_files if f not in preferred]:
            raw = open(os.path.join(folder, cue_name), 'rb').read()
            text = None
            for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if not text:
                continue
            if cue_name in preferred:
                return text, False
            for m in re.finditer(r'^\s*FILE\s+"?(.+?)"?\s+\w+\s*$', text, re.M):
                if os.path.splitext(os.path.basename(m.group(1)))[0] == stem:
                    return text, False
        return None, False

    @staticmethod
    def _cue_time(value):
        m, s, f = (int(x) for x in value.split(':'))
        return m * 60 + s + f / 75

    def _parse_cue(self, text, audio_path, embedded):
        disc, tracks, files, current = {}, [], [], None
        stem = os.path.splitext(os.path.basename(audio_path))[0]

        def unq(v):
            return v.strip().strip('"')
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            word, _, rest = line.partition(' ')
            word = word.upper()
            if word == 'FILE':
                m = re.match(r'"?(.+?)"?\s+\w+$', rest)
                files.append(os.path.splitext(os.path.basename(m.group(1) if m else rest))[0])
                current = None
            elif word == 'TRACK':
                current = {'number': int(rest.split()[0]), 'file_idx': len(files) - 1}
                tracks.append(current)
            elif word in ('TITLE', 'PERFORMER'):
                (current if current is not None else disc)[word.lower()] = unq(rest)
            elif word == 'INDEX' and current is not None:
                idx, _, t = rest.partition(' ')
                current['index' + idx.strip()[-1]] = self._cue_time(t.strip())
            elif word == 'REM':
                key, _, val = rest.partition(' ')
                key = key.upper()
                if current is None and key in ('DATE', 'GENRE'):
                    disc[key.lower()] = unq(val)
                elif current is not None and key == 'REPLAYGAIN_TRACK_GAIN':
                    current['gain'] = unq(val)
                elif current is not None and key == 'REPLAYGAIN_TRACK_PEAK':
                    current['peak'] = unq(val)
        if len(set(files)) > 1 and not embedded:
            idx = [i for i, f in enumerate(files) if f == stem]
            if not idx:
                return disc, []
            tracks = [t for t in tracks if t['file_idx'] in idx]
        tracks = [t for t in tracks if 'index1' in t]
        for i, t in enumerate(tracks):
            t['start'] = t['index1']
            t['end'] = (tracks[i + 1].get('index0', tracks[i + 1]['index1']) if i + 1 < len(tracks) else None)
        return disc, tracks

    def cue_tracks(self, track_info, length=None, choice=None):
        """(pistas, elegida) si el archivo es un disco con cue; (None, None) si no.

        choice: número de pista elegido en el móvil, o 0 para el disco entero.
        Si no se puede saber qué pista es, elegida = None y el móvil pregunta."""
        path = track_info.get('path') or ''
        text, embedded = self._read_cuesheet(path)
        if not text:
            return None, None
        disc, tracks = self._parse_cue(text, path, embedded)
        if len(tracks) < 2:
            return None, None
        chosen = None
        if choice is not None:
            if choice == 0:
                return (disc, tracks), 'whole'
            chosen = next((t for t in tracks if t['number'] == choice), None)
        else:
            def norm(s):
                return re.sub(r'\W+', '', (s or '').lower())
            title = norm(track_info.get('title'))
            if title:
                m = [t for t in tracks if norm(t.get('title')) == title]
                if len(m) == 1:
                    chosen = m[0]
            if not chosen:
                num = (track_info.get('tracknumber') or '').split('/')[0].strip()
                if num.isdigit():
                    chosen = next((t for t in tracks if t['number'] == int(num)), None)
            total = track_info.get('length')
            if not chosen and length and total and length < total - 2:
                m = [t for t in tracks
                     if (t['end'] is not None and abs((t['end'] - t['start']) - length) < 1.5)
                     or (t['end'] is None and abs((total - t['start']) - length) < 1.5)]
                if len(m) == 1:
                    chosen = m[0]
        return (disc, tracks), chosen

    def apply_cue(self, track_info, disc, tracks, chosen):
        artist = chosen.get('performer') or disc.get('performer') or track_info.get('artist', '')
        title = chosen.get('title') or track_info.get('title', '')
        album = disc.get('title') or track_info.get('album', '')
        track_info.update({
            'artist': artist, 'title': title, 'album': album,
            'song_name': f"{artist} - {title}",
            'filename': safe_name(f"{artist} - {title}") + os.path.splitext(track_info['path'])[1],
            'cue': dict(chosen, artist=artist, album=album, date=disc.get('date', ''),
                        genre=disc.get('genre', ''), total_tracks=len(tracks)),
        })
        return track_info

    def extract_cue_track(self, source, cue, dest_path):
        ext = os.path.splitext(source)[1].lower()
        out_ext, codec = ('.flac', ['-c:a', 'flac']) if ext in LOSSLESS_EXTS else (ext, ['-c:a', 'copy'])
        dest_file = os.path.join(dest_path, safe_name(f"{cue['artist']} - {cue['title']}") + out_ext)
        cmd = ['ffmpeg', '-y', '-v', 'error', '-ss', f"{cue['start']:.3f}", '-i', source]
        if cue['end'] is not None:
            cmd += ['-t', f"{cue['end'] - cue['start']:.3f}"]
        cmd += ['-map', '0:a:0', *codec, '-map_metadata', '0']
        metadata = {
            'CUESHEET': '', 'BPM': '', 'INITIALKEY': '',
            'TITLE': cue['title'], 'ARTIST': cue['artist'], 'ALBUM': cue['album'],
            'TRACKNUMBER': f"{cue['number']}/{cue['total_tracks']}",
            'REPLAYGAIN_TRACK_GAIN': cue.get('gain', ''), 'REPLAYGAIN_TRACK_PEAK': cue.get('peak', ''),
        }
        if cue.get('date'):
            metadata['DATE'] = cue['date']
        if cue.get('genre'):
            metadata['GENRE'] = cue['genre']
        for k, v in metadata.items():
            cmd += ['-metadata', f"{k}={v}"]
        cmd.append(dest_file)
        subprocess.run(cmd, check=True)
        return dest_file

    # --- operaciones con archivos ---

    def copy_file(self, source, folder, cue=None):
        dest_path = os.path.join(MIX, folder)
        os.makedirs(dest_path, exist_ok=True)
        if cue:
            dest_file = self.extract_cue_track(source, cue, dest_path)
        else:
            dest_file = os.path.join(dest_path, os.path.basename(source))
            copy_sin_permisos(source, dest_file)
        dest_file = self.analyze_and_convert_flac(dest_file)
        self.add_caratula(dest_file)
        self.fecha_original(dest_file)
        return dest_file

    def move_file(self, source, folder):
        # Regla: solo se mueve lo que ya está en Mix (reordenar); los discos nunca se tocan
        if not self.in_mix(source):
            raise MoverError(f"Solo se mueve lo que ya está en Mix: {source}")
        dest_path = os.path.join(MIX, folder)
        os.makedirs(dest_path, exist_ok=True)
        dest_file = os.path.join(dest_path, os.path.basename(source))
        shutil.move(source, dest_file, copy_function=copy_sin_permisos)
        return dest_file

    def _run_script(self, args, timeout):
        try:
            r = subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=timeout)
            out = (r.stdout.strip() or r.stderr.strip())
            if out:
                self.log(out[-500:])
        except (OSError, subprocess.TimeoutExpired) as e:
            self.log(f"{os.path.basename(args[0])}: {e}")

    def fecha_original(self, dest_file):
        self._run_script([os.path.join(HERE, "fecha_original_mix.py"), "--apply", "--solo-archivo", "--quiet",
                          dest_file], timeout=180)

    def add_caratula(self, dest_file):
        self._run_script([os.path.join(HERE, "embeed_cover.py"), dest_file], timeout=60)

    def analyze_and_convert_flac(self, file_path):
        """FLAC de más de 16 bits / 44100 Hz -> 16 bits / 44100 Hz."""
        if not file_path.lower().endswith(".flac"):
            return file_path
        try:
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                "-show_entries", "stream=sample_rate,bits_per_sample", "-of", "json", file_path],
                               capture_output=True, text=True, check=True)
            stream = json.loads(r.stdout).get("streams", [{}])[0]
            if int(stream.get("bits_per_sample", 0)) > 16 or int(stream.get("sample_rate", 0)) > 44100:
                tmp = f"{file_path}.tmp.flac"
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", file_path, "-ar", "44100",
                                "-sample_fmt", "s16", tmp], check=True)
                os.replace(tmp, file_path)
                self.log("Convertido a 16 bits / 44100 Hz")
        except Exception as e:
            self.log(f"Error analizando/convirtiendo: {e}")
        return file_path

    def set_tags(self, file_path, comment="", rename=True):
        """Comentario (ffmpeg) y renombrado «Artista - Título (año álbum) [sello]»."""
        try:
            tags = ffprobe_tags(file_path)
        except Exception as e:
            self.log(f"No se pudieron leer las etiquetas: {e}")
            return file_path
        if comment:
            ext = os.path.splitext(file_path)[1].lower()
            tmp = f"{file_path}.temp{ext}"
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', file_path, '-map_metadata', '0',
                            '-metadata', f'comment={comment}', '-c', 'copy', tmp], check=True)
            os.replace(tmp, file_path)
        if not rename:
            return file_path
        new_name = nombre_mix(tags.get('artist') or tags.get('album_artist') or tags.get('album artist', ''),
                              tags.get('title', ''), tags.get('album', ''),
                              tags.get('date') or tags.get('year', ''),
                              tags.get('label') or tags.get('publisher') or tags.get('organization', ''))
        if not new_name:
            self.log("Sin artista/título en las etiquetas, no se renombra")
            return file_path
        new_path = os.path.join(os.path.dirname(file_path), new_name + os.path.splitext(file_path)[1].lower())
        if new_path != file_path:
            os.rename(file_path, new_path)
        return new_path

    # --- playlists ---

    def _write_playlist(self, name, folder_path, recursive=True):
        with open(os.path.join(MIX, f"{name}.m3u"), 'w') as f:
            if recursive:
                walker = os.walk(folder_path)
            else:
                try:
                    entries = sorted(os.listdir(folder_path))
                except OSError:
                    entries = []
                walker = [(folder_path, [], [e for e in entries if os.path.isfile(os.path.join(folder_path, e))])]
            for root, _dirs, files in walker:
                for file in sorted(files):
                    if not file.startswith('.') and file.lower().endswith(PLAYLIST_EXTS):
                        f.write(os.path.relpath(os.path.join(root, file), MIX) + "\n")

    def _write_aggregate(self, name, folders):
        tracks = set()
        for folder in folders:
            folder_path = os.path.join(MIX, folder)
            if not os.path.isdir(folder_path):
                continue
            for root, _dirs, files in os.walk(folder_path):
                tracks.update(os.path.relpath(os.path.join(root, f), MIX) for f in files
                              if not f.startswith('.') and f.lower().endswith(PLAYLIST_EXTS))
        with open(os.path.join(MIX, f"{name}.m3u"), 'w') as f:
            f.write("#AGREGADA\n")
            f.writelines(f"{t}\n" for t in sorted(tracks))

    def update_playlists(self):
        for m3u in [f for f in os.listdir(MIX) if f.endswith('.m3u')]:
            os.unlink(os.path.join(MIX, m3u))
        for top, children in self.folder_tree().items():
            top_path = os.path.join(MIX, top)
            self._write_playlist(top, top_path, recursive=not children)
            for child in children or []:
                self._write_playlist(child, os.path.join(top_path, child))
            if children and AGREGADAS_ACTIVAS:
                self._write_aggregate(f"{top}_todo", [top])
        if AGREGADAS_ACTIVAS:
            for name, folders in PLAYLISTS_AGREGADAS.items():
                self._write_aggregate(name, folders)

    # --- ListenBrainz ---

    def listenbrainz(self, path, info, pin=False):
        """Marca la canción como favorita o, con «pin», la fija en el perfil (lo que en Last.fm es la obsesión).

        None si no hay LISTENBRAINZ_TOKEN."""
        token = os.getenv("LISTENBRAINZ_TOKEN")
        if not token:
            return None
        api, headers = "https://api.listenbrainz.org/1/", {"Authorization": f"Token {token}"}
        try:
            mbid = self._recording_mbid(path, info, api, headers)
            if not mbid:
                self.log(f"ListenBrainz: {info['song_name']} no está en MusicBrainz")
                return False
            if pin:
                r = requests.post(api + "pin", json={"recording_mbid": mbid}, headers=headers, timeout=20)
            else:
                r = requests.post(api + "feedback/recording-feedback", json={"recording_mbid": mbid, "score": 1},
                                  headers=headers, timeout=20)
            if r.status_code != 200:
                self.log(f"ListenBrainz: HTTP {r.status_code} {r.text[:200]}")
            return r.status_code == 200
        except Exception as e:
            self.log(f"ListenBrainz: {e}")
            return False

    @staticmethod
    def _recording_mbid(path, info, api, headers):
        """MBID de la grabación: la etiqueta de Picard o, si no la lleva, la búsqueda de ListenBrainz."""
        # En un disco con cue la etiqueta es la del fichero entero, no la de la pista
        if not info.get('cue'):
            try:
                tags = mutagen.File(path, easy=True) or {}
                mbid = (tags.get('musicbrainz_trackid') or [''])[0].strip()
                if re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', mbid, re.I):
                    return mbid.lower()
            except Exception:
                pass
        if not (info.get('artist') and info.get('title')):
            return None
        r = requests.get(api + "metadata/lookup/", headers=headers, timeout=20,
                         params={"artist_name": info['artist'], "recording_name": info['title']})
        r.raise_for_status()
        return r.json().get('recording_mbid')

    # --- Last.fm (opcional, API de pago) ---

    @staticmethod
    def _lastfm_sig(params, secret):
        s = "".join(f"{k}{v}" for k, v in sorted(params.items()) if k != "format")
        return hashlib.md5((s + secret).encode("utf-8")).hexdigest()

    def lastfm_love(self, artist, title):
        key, secret = os.getenv("LASTFM_API_KEY"), os.getenv("LASTFM_API_SECRET")
        user, password = os.getenv("LASTFM_USERNAME"), os.getenv("LASTFM_PASSWORD")
        if not all([key, secret, user, password]):
            return None  # Last.fm sin configurar: no se intenta
        url = "https://ws.audioscrobbler.com/2.0/"
        try:
            p = {"method": "auth.getMobileSession", "username": user, "password": password,
                 "api_key": key, "format": "json"}
            p["api_sig"] = self._lastfm_sig(p, secret)
            sk = requests.post(url, data=p, timeout=20).json().get("session", {}).get("key")
            if not sk:
                self.log("Last.fm: no se pudo obtener la sesión")
                return False
            p = {"method": "track.love", "artist": artist, "track": title, "api_key": key, "sk": sk,
                 "format": "json"}
            p["api_sig"] = self._lastfm_sig(p, secret)
            return requests.post(url, data=p, timeout=20).status_code == 200
        except Exception as e:
            self.log(f"Last.fm: {e}")
            return False

    def lastfm_obsession(self, artist, title):
        """«Set as current obsession» por la web (no hay API), como en el escritorio."""
        import lxml.html
        user, password = os.getenv("LASTFM_USERNAME"), os.getenv("LASTFM_PASSWORD")
        if not (user and password):
            return False
        base = "https://www.last.fm"
        s = requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"

        def call(method, url, **kw):
            last = None
            headers = {"Connection": "close", **kw.pop("headers", {})}
            for _ in range(4):
                try:
                    return s.request(method, url, timeout=20, headers=headers, **kw)
                except requests.RequestException as e:
                    last = e
                    time.sleep(1.5)
            raise last

        def form_fields(form):
            data = {}
            for el in form.xpath(".//input[@name] | .//textarea[@name] | .//select[@name]"):
                if el.tag == "input" and el.get("type") in ("checkbox", "radio") and el.get("checked") is None:
                    continue
                data[el.get("name")] = el.value or ""
            return data

        try:
            call("GET", f"{base}/login")
            call("POST", f"{base}/login",
                 data={"csrfmiddlewaretoken": s.cookies.get("csrftoken", ""),
                       "username_or_email": user, "password": password},
                 headers={"Referer": f"{base}/login"})
            if "sessionid" not in s.cookies.get_dict():
                self.log("Last.fm: no se pudo iniciar sesión en la web")
                return False
            track_url = f"{base}/music/{urllib.parse.quote_plus(artist)}/_/{urllib.parse.quote_plus(title)}"
            page = lxml.html.fromstring(call("GET", track_url).text)
            forms = page.xpath('//form[contains(@action, "/obsessions")]')
            if not forms:
                self.log("Last.fm: la canción no tiene «Set as current obsession»")
                return False
            hdr = {"Referer": track_url, "X-Requested-With": "XMLHttpRequest"}
            r = call("POST", urllib.parse.urljoin(base, forms[0].get("action")), data=form_fields(forms[0]), headers=hdr)
            if r.status_code >= 400:
                return False
            if "<form" in r.text:
                confirm = lxml.html.fromstring(r.text).xpath('//form[contains(@action, "/obsessions")]')
                if confirm:
                    r = call("POST", urllib.parse.urljoin(base, confirm[0].get("action")),
                             data=form_fields(confirm[0]), headers=hdr)
                    if r.status_code >= 400:
                        return False
            return True
        except Exception as e:
            self.log(f"Last.fm obsesión: {e}")
            return False

    # ── lo que pide el móvil ───────────────────────────────────────────────

    def analyze(self, song_id=None, length=None, cue_choice=None, path=None, artist=None, title=None, album=None):
        """Todo lo que enseña el diálogo del escritorio antes de elegir carpeta."""
        path = self.resolve(song_id, path, artist, title, album)
        info = self.track_info(path)
        reclassify = self.in_mix(path)
        tree = self.folder_tree()

        current_folder = None
        force_supergenre = None
        if reclassify:
            current_folder = os.path.relpath(os.path.dirname(path), MIX)
            parent = os.path.dirname(path)
            if os.path.normpath(os.path.dirname(parent)) == os.path.normpath(MIX):
                name = os.path.basename(parent)
                canonical = next((k for k in tree if k.lower() == name.lower()), None)
                if canonical and tree.get(canonical):
                    force_supergenre = canonical

        cue = None
        duplicates = []
        if not reclassify:
            found, chosen = self.cue_tracks(info, length, cue_choice)
            if found:
                disc, tracks = found
                cue = {'tracks': [{'number': t['number'], 'title': t.get('title', ''),
                                   'duration': (t['end'] - t['start']) if t['end'] is not None else None}
                                  for t in tracks],
                       'chosen': (0 if chosen == 'whole' else chosen['number']) if chosen else None}
                if chosen and chosen != 'whole':
                    self.apply_cue(info, disc, tracks, chosen)
            duplicates = self.find_duplicates(info['song_name'], info.get('title', ''))

        album_row = self.find_album_row(info)
        try:
            stats = self.mix_stats(info.get('artist', ''), info.get('label', ''),
                                   exclude_path=path if reclassify else None)
        except Exception as e:
            self.log(f"Error contando canciones en Mix: {e}")
            stats = {'artist': [], 'label': []}

        existing_comment = ""
        if reclassify:
            existing_comment = read_comment(path)
        elif duplicates:
            existing_comment = read_comment(duplicates[0])

        year = str((album_row or {}).get('year') or '')
        expected = nombre_mix(info.get('artist'), info.get('title'), info.get('album'),
                              info.get('date') or year, info.get('label')) or info['song_name']

        mine = artist_names(info.get('artist', ''))
        return {
            'artist': info.get('artist', ''), 'title': info.get('title', ''), 'album': info.get('album', ''),
            'path': path, 'reclassify': reclassify, 'current_folder': current_folder,
            'force_supergenre': force_supergenre,
            'expected_filename': expected,
            'duplicates': [{'path': os.path.relpath(d, MIX),
                            'same_artist': bool(mine & artist_names(os.path.basename(d).split(' - ')[0]))}
                           for d in duplicates],
            'existing_comment': existing_comment,
            'cue': cue,
            'tree': self.tree_with_counts(),
            'stats': {'artist': stats['artist'], 'label': stats['label'], 'label_name': info.get('label', ''),
                      'genres': self.album_genres(album_row), 'credits': self.album_credits(album_row)},
        }

    def execute(self, song_id=None, folder="", comment="", obsession=False, action=None, length=None,
                cue_choice=None, path=None):
        """Copiar a Mix (o mover, si ya está en Mix) como el escritorio. Devuelve los avisos.

        Lo que no está en Mix SOLO se copia: «action» ya no existe (antes permitía mover la
        copia de Mix encontrada como duplicada); se acepta por compatibilidad y se rechaza «move»."""
        msgs = []
        folder = os.path.normpath(folder).strip('/')
        if folder.startswith('..') or not folder:
            raise MoverError("Carpeta no válida")
        path = self.resolve(song_id, path)
        info = self.track_info(path)
        reclassify = self.in_mix(path)
        new_file = None
        if action == 'move' and not reclassify:
            raise MoverError("Esta canción no está en Mix: solo se puede copiar")

        if reclassify:
            destino = os.path.normpath(os.path.join(MIX, folder))
            if destino.lower() == os.path.normpath(os.path.dirname(path)).lower():
                self.set_tags(path, comment, rename=False)
                msgs.append(f"Sin cambios: ya está en {folder}")
            else:
                new_file = self.move_file(path, folder)
                new_file = self.set_tags(new_file, comment, rename=True)
                msgs.append(f"Reclasificado en {self.folder_summary(folder)}")
        else:
            found, chosen = self.cue_tracks(info, length, cue_choice)
            if found and chosen and chosen != 'whole':
                self.apply_cue(info, *found, chosen)
            elif found and not chosen:
                raise MoverError("Disco con cue: elige la pista")
            new_file = self.copy_file(path, folder, cue=info.get('cue'))
            new_file = self.set_tags(new_file, comment, rename=True)
            msgs.append(f"Copiado a {self.folder_summary(folder)}")
            loved = self.listenbrainz(new_file, info)
            if loved is not None:
                msgs.append("ListenBrainz: añadida a favoritas" if loved else "ListenBrainz: error al añadir a favoritas")
            loved = self.lastfm_love(info['artist'], info['title'])
            if loved is not None:
                msgs.append("Last.fm: añadida a loved" if loved else "Last.fm: error al añadir a loved")

        if obsession:
            pinned = self.listenbrainz(new_file or path, info, pin=True)
            if pinned is not None:
                msgs.append("ListenBrainz: fijada en el perfil" if pinned else "ListenBrainz: error al fijarla")
            # Sin ListenBrainz se intenta siempre, para avisar de que falta configurarlo
            if pinned is None or os.getenv("LASTFM_USERNAME"):
                msgs.append("Last.fm: marcada como obsesión" if self.lastfm_obsession(info['artist'], info['title'])
                            else "Last.fm: error al marcar la obsesión")

        self.update_playlists()
        if new_file:
            msgs.append(os.path.relpath(new_file, MIX))
        return msgs
