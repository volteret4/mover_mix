# mover_mix

Una API para mandar la canción que estás escuchando a tu carpeta de DJ (**Mix**) y clasificarla en su subcarpeta, desde el móvil o desde el PC, sin abrir el gestor de archivos.

La canción llega como id de Navidrome (lo que da un cliente Subsonic), como ruta o como artista + título. mover_mix:

1. **La copia** a la subcarpeta de Mix que elijas. Lo que no está en Mix solo se copia: tu biblioteca se monta en solo lectura. Mover solo sirve para reclasificar algo que ya está en Mix.
2. Si es un **disco entero con cue**, extrae solo esa pista.
3. Pasa a 16 bits / 44,1 kHz los FLAC de más resolución.
4. La **renombra** `Artista - Título (año álbum) [sello]` y le pone el comentario que escribas.
5. Le pone la **fecha original** de la canción (MusicBrainz y Discogs; si discrepan, no la toca) y la **portada**.
6. **Regenera los .m3u** de Mix: uno por carpeta, y por subcarpeta en las que tienen subcarpetas.
7. Opcional: la marca como *loved* en Last.fm, y como *current obsession* si lo pides.

Antes de elegir carpeta, `GET /api/analyze` te dice si ya la tienes en Mix (posibles duplicados), cuántas canciones hay de ese artista y ese sello en cada carpeta, y los géneros y créditos del disco si tienes la base de music-fuzzy.

**Ojo:** al regenerar las listas, mover_mix borra **todos** los `.m3u` de la raíz de Mix y los vuelve a escribir. No guardes ahí listas hechas a mano.

## Instalación

Necesitas Docker y [Navidrome](https://www.navidrome.org/) leyendo tu biblioteca (de su base se saca la ruta real de cada canción).

```bash
git clone https://github.com/volteret4/mover_mix.git && cd mover_mix && cp .env.example .env
```

En `.env`:

- `MOVER_MIX_TOKEN`: una clave larga; los clientes la mandan en la cabecera `X-Token`.
- `MIX`, `MUSICA` y `NAVIDROME_DATA`: tus carpetas.
- `LIBRARY_ROOTS`: cómo se llaman, dentro del contenedor de Navidrome, las carpetas de cada biblioteca, y dónde están aquí. Si Navidrome ve tu música en `/music` y tu Mix en `/mix`, déjalo como está.

```bash
docker compose up -d --build
```

Comprueba que responde:

```bash
curl http://localhost:8786/api/health
```

La API solo acepta peticiones con el token. Aun así, ábrela solo en tu red o por VPN.

## API

Todas las llamadas llevan la cabecera `X-Token`.

| Llamada | Qué hace |
|---|---|
| `GET /api/analyze?id=…` (o `path=…`, o `artist=…&title=…[&album=…]`) | Lo que hace falta para decidir: ruta, si ya está en Mix, duplicados, árbol de carpetas con recuentos, nombre que tendrá, pistas del cue, géneros… |
| `POST /api/move` con `{"id" o "path", "folder", "comment", "obsession", "length", "cue"}` | Copia (o reclasifica) en segundo plano y devuelve `{"job": …}` |
| `GET /api/jobs/<job>` | Estado (`queued`, `running`, `done`, `error`), avisos y log |
| `POST /api/vvmm` con `{"artist", "album", "title", "tags"}` | Opcional: crea un post de blog con [vvmm](https://github.com/volteret4/vvmm) |
| `GET /api/health` | Sin token |

Ejemplo:

```bash
curl -H "X-Token: $MOVER_MIX_TOKEN" 'http://localhost:8786/api/analyze?artist=Burial&title=Archangel'
```

```bash
curl -H "X-Token: $MOVER_MIX_TOKEN" -H 'Content-Type: application/json' -d '{"path": "/music/Burial/Untrue/02 Archangel.flac", "folder": "Electronica/dubstep", "comment": "nocturna"}' http://localhost:8786/api/move
```

Las operaciones van de una en una, porque todas reescriben los `.m3u`.

## Clientes

- **Escritorio:** `mover_mix_playerctl.py` en [Scripts](https://github.com/volteret4/Scripts/tree/main/Musica/MoverMusica) toma la canción que suena (playerctl) y te deja elegir carpeta. Con `MOVER_MIX_URL` y `MOVER_MIX_TOKEN` en su `.env`, delega en esta API.
- **Cualquier otro:** basta con poder hacer peticiones HTTP con una cabecera, por ejemplo Tasker, un atajo de iOS o un cliente Subsonic modificado.

## Opcional

| Qué | Variables |
|---|---|
| Géneros, créditos y año del disco | `MFUZZ_DIR`: carpeta con `musica_local.sqlite` de music-fuzzy (o creada con `crear_musica_local.py` de [mapa_discos](https://github.com/volteret4/mapa_discos)) |
| Last.fm | `LASTFM_API_KEY`, `LASTFM_API_SECRET`, `LASTFM_USERNAME`, `LASTFM_PASSWORD` |
| Fecha original desde Discogs | `DISCOGS_TOKEN` |
| Rutas de los `.m3u` iguales que en tu PC | Monta Mix en la misma ruta que en el PC y pon esa ruta en `MIX_PATH` |
| Posts con vvmm | Monta vvmm en `/vvmm` y tu blog en `/blog` (ver `docker-compose.yml`) |

Es un módulo de [REPEAT-ONE](https://volteret4.github.io/repeat-one-web/), y funciona igual por separado.
