# mover_mix

FastAPI (`app.py`) sobre `mover.py`. Token obligatorio (`MOVER_MIX_TOKEN`, cabecera `X-Token`).

- **Regla:** lo que no está en Mix solo se COPIA; mover es solo para reclasificar dentro de Mix. Se comprueba en `execute()` y en `move_file()`, y la biblioteca se monta `:ro`.
- Ruta real de una canción: base de Navidrome (`NAVIDROME_DB`, solo lectura) → `LIBRARY_ROOTS[library.path] + media_file.path`.
- `update_playlists()` borra y regenera todos los `.m3u` de la raíz de `MIX_PATH`. Una operación a la vez (`LOCK`).
- `fecha_original_mix.py` y `embeed_cover.py` se lanzan como subprocesos sobre el fichero copiado; su caché va en `$HOME` (= `/data`).
- vvmm es opcional: `/api/vvmm` responde 501 si no hay `main.py` en `VVMM_DIR`.
- El código va dentro de la imagen: tras cambiarlo, `docker compose up -d --build`.
- Nada de rutas, usuarios ni claves concretas en el código: todo por entorno (`.env.example`).
