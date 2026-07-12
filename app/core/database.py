"""
Gestion de la base de données SQLite pour le cache des modèles
"""
import aiosqlite
import json
import re
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
from ..logger import logger

class Database:
    # Timeout par défaut (ms) pour l'attente d'un verrou SQLite avant "database is locked"
    BUSY_TIMEOUT_MS = 10000

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._initialized = False

    @staticmethod
    def _normalize_source_type(source_type: Optional[str]) -> str:
        return (source_type or "").strip().lower() or "chaturbate"

    @staticmethod
    def _default_record_path(username: str) -> str:
        model_folder = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(username or "").strip())
        model_folder = model_folder.strip(".-") or "model"
        return f"{model_folder}/videos/record"

    def _connect(self):
        """Ouvre une connexion aiosqlite avec timeout de verrou.

        Retourne un context manager async. Le timeout Python évite les erreurs
        immédiates "database is locked" quand plusieurs tâches écrivent/lisent
        en parallèle (monitoring, sync, conversion, endpoints API).
        """
        # timeout (s): délai max d'attente d'un verrou avant OperationalError
        return aiosqlite.connect(self.db_path, timeout=self.BUSY_TIMEOUT_MS / 1000)

    async def _apply_pragmas(self, db):
        """Active WAL + busy_timeout sur une connexion."""
        await db.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")

    async def initialize(self):
        """Initialise la base de données et crée les tables"""
        if self._initialized:
            return

        async with self._connect() as db:
            # Activer WAL : lectures non bloquées par les écritures concurrentes
            # (résout les 500 sur /api/models et /api/following sous charge)
            try:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA synchronous=NORMAL")
            except Exception as e:
                logger.warning("Impossible d'activer WAL", error=str(e))
            await self._apply_pragmas(db)
            # Table pour les modèles et leur statut
            await db.execute("""
                CREATE TABLE IF NOT EXISTS models (
                    username TEXT NOT NULL,
                    display_name TEXT,
                    is_online BOOLEAN DEFAULT 0,
                    is_recording BOOLEAN DEFAULT 0,
                    viewers INTEGER DEFAULT 0,
                    thumbnail_path TEXT,
                    thumbnail_updated_at INTEGER,
                    last_check_at INTEGER,
                    auto_record BOOLEAN DEFAULT 1,
                    record_quality TEXT DEFAULT 'best',
                    retention_days INTEGER DEFAULT 30,
                    record_path TEXT,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    room_status TEXT,
                    created_at INTEGER,
                    updated_at INTEGER,
                    PRIMARY KEY(username, source_type)
                )
            """)

            # Fiches locales enrichies pour la médiathèque. Ces informations
            # restent séparées des réglages de stream dans models.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_profiles (
                    username TEXT PRIMARY KEY,
                    display_name TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    age INTEGER,
                    birth_date TEXT,
                    profile_image_url TEXT,
                    profile_image_source_url TEXT,
                    profile_image_path TEXT,
                    address TEXT,
                    city TEXT,
                    region TEXT,
                    postal_code TEXT,
                    country TEXT,
                    aliases TEXT,
                    tags TEXT,
                    notes TEXT,
                    social_urls TEXT,
                    stream_urls TEXT,
                    profile_urls TEXT,
                    created_at INTEGER,
                    updated_at INTEGER
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS media_profile_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_username TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    channel_username TEXT NOT NULL,
                    channel_url TEXT,
                    auto_record BOOLEAN DEFAULT 1,
                    record_quality TEXT DEFAULT 'best',
                    retention_days INTEGER DEFAULT 30,
                    record_path TEXT,
                    created_at INTEGER,
                    updated_at INTEGER,
                    UNIQUE(profile_username, source_type, channel_username)
                )
            """)
            
            # Table pour les rediffusions
            await db.execute("""
                CREATE TABLE IF NOT EXISTS recordings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    recording_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER,
                    duration_seconds INTEGER,
                    thumbnail_path TEXT,
                    mp4_path TEXT,
                    mp4_size INTEGER,
                    is_converted BOOLEAN DEFAULT 0,
                    media_kind TEXT DEFAULT 'recording',
                    title TEXT,
                    import_status TEXT,
                    import_error TEXT,
                    source_mtime INTEGER,
                    playable_path TEXT,
                    playable_size INTEGER,
                    protected_from_retention BOOLEAN DEFAULT 0,
                    created_at INTEGER,
                    UNIQUE(username, filename)
                )
            """)
            
            # Table pour l'authentification Chaturbate
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chaturbate_auth (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    username TEXT,
                    password_hash TEXT,
                    is_logged_in BOOLEAN DEFAULT 0,
                    session_cookies TEXT,
                    cf_clearance TEXT,
                    csrf_token TEXT,
                    last_login_at INTEGER,
                    last_error TEXT,
                    updated_at INTEGER
                )
            """)

            # Table pour les modèles suivis sur Chaturbate
            await db.execute("""
                CREATE TABLE IF NOT EXISTS followed_models (
                    username TEXT NOT NULL,
                    display_name TEXT,
                    is_online BOOLEAN DEFAULT 0,
                    viewers INTEGER DEFAULT 0,
                    thumbnail_url TEXT,
                    last_seen_online_at INTEGER,
                    synced_at INTEGER,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    room_status TEXT,
                    PRIMARY KEY(username, source_type)
                )
            """)

            # Table pour les paramètres (tags blacklistés, etc.)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at INTEGER
                )
            """)

            # Table pour la position de lecture (reprise)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS playback_positions (
                    recording_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    position_seconds REAL DEFAULT 0,
                    duration_seconds REAL DEFAULT 0,
                    watched_at INTEGER,
                    updated_at INTEGER
                )
            """)

            # Table pour les plugins installés (sources de streaming tierces)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS plugins (
                    id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    source_type TEXT NOT NULL UNIQUE,
                    source_repo TEXT,
                    enabled BOOLEAN DEFAULT 1,
                    installed BOOLEAN DEFAULT 1,
                    status TEXT DEFAULT 'pending_restart',
                    last_error TEXT,
                    manifest_json TEXT,
                    installed_at INTEGER,
                    updated_at INTEGER
                )
            """)

            # Sessions generiques par provider. Credentials are local-only and
            # allow reconnect retries after provider sessions expire.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS provider_sessions (
                    source_type TEXT PRIMARY KEY,
                    username TEXT,
                    is_logged_in BOOLEAN DEFAULT 0,
                    session_cookies TEXT,
                    local_storage TEXT,
                    credential_username TEXT,
                    credential_password TEXT,
                    credentials_updated_at INTEGER,
                    last_login_at INTEGER,
                    last_error TEXT,
                    updated_at INTEGER
                )
            """)

            # Index pour les requêtes fréquentes
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_models_online
                ON models(is_online, username)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_models_source_username
                ON models(source_type, username)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_followed_source_username
                ON followed_models(source_type, username)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_recordings_username
                ON recordings(username, created_at DESC)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_media_profile_sources_profile
                ON media_profile_sources(profile_username)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_media_profile_sources_auto
                ON media_profile_sources(auto_record, source_type, channel_username)
            """)

            # Migrations idempotentes pour schémas existants
            await self._migrate_schema(db)
            await self._migrate_provider_identity_keys(db)

            await db.commit()

        self._initialized = True
        logger.info("Base de données initialisée", db_path=str(self.db_path))

    async def _migrate_schema(self, db):
        """Ajoute les colonnes manquantes sur les DB existantes (migrations légères)."""
        migrations = [
            ("recordings", "conversion_attempts", "INTEGER DEFAULT 0"),
            ("recordings", "conversion_error", "TEXT"),
            ("recordings", "last_conversion_attempt", "INTEGER"),
            ("recordings", "media_kind", "TEXT DEFAULT 'recording'"),
            ("recordings", "title", "TEXT"),
            ("recordings", "import_status", "TEXT"),
            ("recordings", "import_error", "TEXT"),
            ("recordings", "source_mtime", "INTEGER"),
            ("recordings", "playable_path", "TEXT"),
            ("recordings", "playable_size", "INTEGER"),
            ("recordings", "protected_from_retention", "BOOLEAN DEFAULT 0"),
            ("media_profiles", "birth_date", "TEXT"),
            ("media_profiles", "profile_image_url", "TEXT"),
            ("media_profiles", "profile_image_source_url", "TEXT"),
            ("media_profiles", "profile_image_path", "TEXT"),
            ("models", "source_type", "TEXT DEFAULT 'chaturbate'"),
            ("models", "room_status", "TEXT"),
            ("models", "record_path", "TEXT"),
            ("followed_models", "source_type", "TEXT DEFAULT 'chaturbate'"),
            ("followed_models", "room_status", "TEXT"),
            ("provider_sessions", "credential_username", "TEXT"),
            ("provider_sessions", "credential_password", "TEXT"),
            ("provider_sessions", "credentials_updated_at", "INTEGER"),
            ("playback_positions", "watched_at", "INTEGER"),
        ]
        for table, column, ddl in migrations:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                logger.info("Migration: colonne ajoutée", table=table, column=column)
            except Exception:
                # La colonne existe déjà - SQLite lève "duplicate column"
                pass

    async def _migrate_provider_identity_keys(self, db):
        """Upgrade legacy username-only tables to provider-aware identities."""

        async def table_pk_columns(table: str) -> list[str]:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            return [
                row[1]
                for row in sorted(
                    [row for row in rows if int(row[5] or 0) > 0],
                    key=lambda item: int(item[5] or 0),
                )
            ]

        async def rebuild_models() -> None:
            await db.execute("ALTER TABLE models RENAME TO models_legacy_provider_identity")
            await db.execute("""
                CREATE TABLE models (
                    username TEXT NOT NULL,
                    display_name TEXT,
                    is_online BOOLEAN DEFAULT 0,
                    is_recording BOOLEAN DEFAULT 0,
                    viewers INTEGER DEFAULT 0,
                    thumbnail_path TEXT,
                    thumbnail_updated_at INTEGER,
                    last_check_at INTEGER,
                    auto_record BOOLEAN DEFAULT 1,
                    record_quality TEXT DEFAULT 'best',
                    retention_days INTEGER DEFAULT 30,
                    record_path TEXT,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    room_status TEXT,
                    created_at INTEGER,
                    updated_at INTEGER,
                    PRIMARY KEY(username, source_type)
                )
            """)
            await db.execute("""
                INSERT OR REPLACE INTO models (
                    username, display_name, is_online, is_recording, viewers,
                    thumbnail_path, thumbnail_updated_at, last_check_at,
                    auto_record, record_quality, retention_days, record_path, source_type,
                    room_status, created_at, updated_at
                )
                SELECT
                    legacy.username, legacy.display_name, legacy.is_online,
                    legacy.is_recording, legacy.viewers, legacy.thumbnail_path,
                    legacy.thumbnail_updated_at, legacy.last_check_at,
                    legacy.auto_record, legacy.record_quality,
                    legacy.retention_days, legacy.record_path,
                    CASE
                        WHEN COALESCE(NULLIF(legacy.source_type, ''), 'chaturbate') = 'chaturbate'
                         AND EXISTS (
                            SELECT 1 FROM followed_models fm
                            WHERE fm.username = legacy.username
                              AND fm.source_type = 'cam4'
                         )
                         AND NOT EXISTS (
                            SELECT 1 FROM followed_models fm
                            WHERE fm.username = legacy.username
                              AND fm.source_type = 'chaturbate'
                         )
                        THEN 'cam4'
                        ELSE COALESCE(NULLIF(legacy.source_type, ''), 'chaturbate')
                    END,
                    legacy.room_status, legacy.created_at, legacy.updated_at
                FROM models_legacy_provider_identity AS legacy
                WHERE legacy.username IS NOT NULL AND legacy.username != ''
            """)
            await db.execute("DROP TABLE models_legacy_provider_identity")

        async def rebuild_followed() -> None:
            await db.execute("ALTER TABLE followed_models RENAME TO followed_models_legacy_provider_identity")
            await db.execute("""
                CREATE TABLE followed_models (
                    username TEXT NOT NULL,
                    display_name TEXT,
                    is_online BOOLEAN DEFAULT 0,
                    viewers INTEGER DEFAULT 0,
                    thumbnail_url TEXT,
                    last_seen_online_at INTEGER,
                    synced_at INTEGER,
                    source_type TEXT NOT NULL DEFAULT 'chaturbate',
                    room_status TEXT,
                    PRIMARY KEY(username, source_type)
                )
            """)
            await db.execute("""
                INSERT OR REPLACE INTO followed_models (
                    username, display_name, is_online, viewers, thumbnail_url,
                    last_seen_online_at, synced_at, source_type, room_status
                )
                SELECT
                    username, display_name, is_online, viewers, thumbnail_url,
                    last_seen_online_at, synced_at,
                    COALESCE(NULLIF(source_type, ''), 'chaturbate'),
                    room_status
                FROM followed_models_legacy_provider_identity
                WHERE username IS NOT NULL AND username != ''
            """)
            await db.execute("DROP TABLE followed_models_legacy_provider_identity")

        try:
            if await table_pk_columns("models") != ["username", "source_type"]:
                await rebuild_models()
                logger.info("Migration: models utilise username+source_type")
        except Exception as exc:
            logger.warning("Migration provider-aware models échouée", error=str(exc))

        try:
            if await table_pk_columns("followed_models") != ["username", "source_type"]:
                await rebuild_followed()
                logger.info("Migration: followed_models utilise username+source_type")
        except Exception as exc:
            logger.warning("Migration provider-aware followed_models échouée", error=str(exc))

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_models_online
            ON models(is_online, username)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_models_source_username
            ON models(source_type, username)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_followed_source_username
            ON followed_models(source_type, username)
        """)
    
    async def add_or_update_model(
        self, 
        username: str,
        display_name: Optional[str] = None,
        auto_record: bool = True,
        record_quality: str = "best",
        retention_days: int = 30,
        record_path: Optional[str] = None,
        source_type: Optional[str] = None,
    ):
        """Ajoute ou met à jour un modèle"""
        await self.initialize()
        
        now = int(datetime.now().timestamp())
        source_type = self._normalize_source_type(source_type)
        
        async with self._connect() as db:
            await db.execute("""
                INSERT INTO models (
                    username, display_name, auto_record, record_quality, 
                    retention_days, record_path, source_type, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, source_type) DO UPDATE SET
                    display_name = COALESCE(?, display_name),
                    auto_record = ?,
                    record_quality = ?,
                    retention_days = ?,
                    record_path = COALESCE(?, record_path),
                    updated_at = ?
            """, (
                username, display_name, auto_record, record_quality,
                retention_days, record_path, source_type, now, now,
                display_name, auto_record, record_quality, retention_days, record_path, now
            ))
            await db.commit()
        
        logger.debug("Modèle ajouté/mis à jour", username=username, source_type=source_type)

    async def reconcile_model_sources_from_followed(self) -> int:
        """Repair only genuinely source-less legacy rows.

        ``source_type`` is part of the current model identity. A Chaturbate row
        must never be rewritten merely because another provider follows the same
        username. Old username-only CAM4 rows are repaired once during schema
        migration instead.
        """
        await self.initialize()

        async with self._connect() as db:
            cursor = await db.execute("""
                UPDATE models
                SET source_type = (
                    SELECT fm.source_type
                    FROM followed_models fm
                    WHERE fm.username = models.username
                      AND fm.source_type IS NOT NULL
                      AND fm.source_type != ''
                      AND fm.source_type != 'chaturbate'
                    LIMIT 1
                )
                WHERE (source_type IS NULL OR source_type = '')
                  AND EXISTS (
                    SELECT 1
                    FROM followed_models fm
                    WHERE fm.username = models.username
                      AND fm.source_type IS NOT NULL
                      AND fm.source_type != ''
                      AND fm.source_type != 'chaturbate'
                  )
            """)
            await db.commit()
            return cursor.rowcount or 0
    
    async def update_model_status(
        self,
        username: str,
        is_online: bool,
        viewers: int = 0,
        is_recording: bool = False,
        thumbnail_path: Optional[str] = None,
        room_status: Optional[str] = None,
        source_type: Optional[str] = None,
    ):
        """Met à jour le statut d'un modèle"""
        await self.initialize()

        now = int(datetime.now().timestamp())

        async with self._connect() as db:
            update_fields = {
                'is_online': is_online,
                'viewers': viewers,
                'is_recording': is_recording,
                'room_status': room_status,
                'last_check_at': now,
                'updated_at': now
            }

            if thumbnail_path:
                update_fields['thumbnail_path'] = thumbnail_path
                update_fields['thumbnail_updated_at'] = now

            placeholders = ', '.join(f"{k} = ?" for k in update_fields.keys())
            values = list(update_fields.values()) + [username]
            if source_type:
                values.append(self._normalize_source_type(source_type))
                await db.execute(
                    f"UPDATE models SET {placeholders} WHERE username = ? AND source_type = ?",
                    values,
                )
            else:
                await db.execute(
                    f"UPDATE models SET {placeholders} WHERE username = ?",
                    values,
                )
            await db.commit()
    
    async def get_model(
        self, username: str, source_type: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Récupère les informations d'un modèle"""
        await self.initialize()
        
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if source_type:
                cursor = await db.execute(
                    "SELECT * FROM models WHERE username = ? AND source_type = ?",
                    (username, self._normalize_source_type(source_type)),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM models
                    WHERE username = ?
                    ORDER BY CASE WHEN source_type = 'chaturbate' THEN 0 ELSE 1 END, source_type
                    LIMIT 1
                    """,
                    (username,),
                )
            row = await cursor.fetchone()
            
            if row:
                return dict(row)
            return None
    
    async def get_all_models(self) -> List[Dict[str, Any]]:
        """Récupère tous les modèles"""
        await self.initialize()
        
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM models ORDER BY username"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def get_models_for_auto_record(self) -> List[Dict[str, Any]]:
        """Récupère les modèles avec auto-record activé"""
        await self.initialize()
        
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM models WHERE auto_record = 1 ORDER BY username"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def delete_model(self, username: str, source_type: Optional[str] = None):
        """Supprime un modèle"""
        await self.initialize()
        
        async with self._connect() as db:
            if source_type:
                await db.execute(
                    "DELETE FROM models WHERE username = ? AND source_type = ?",
                    (username, self._normalize_source_type(source_type)),
                )
            else:
                await db.execute("DELETE FROM models WHERE username = ?", (username,))
            await db.commit()
        
        logger.info("Modèle supprimé", username=username, source_type=source_type)

    @staticmethod
    def _json_list(value: Any) -> str:
        if value is None:
            return "[]"
        if isinstance(value, str):
            values = [line.strip() for line in value.splitlines() if line.strip()]
        elif isinstance(value, list):
            values = [str(item).strip() for item in value if str(item).strip()]
        else:
            values = []
        return json.dumps(values, ensure_ascii=False)

    @staticmethod
    def _decode_json_list(value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                return [str(item) for item in decoded if str(item).strip()]
        except Exception:
            pass
        return [line.strip() for line in str(value).splitlines() if line.strip()]

    def _format_media_profile_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(row)
        data["social_urls"] = self._decode_json_list(data.get("social_urls"))
        data["stream_urls"] = self._decode_json_list(data.get("stream_urls"))
        data["profile_urls"] = self._decode_json_list(data.get("profile_urls"))
        return data

    def _format_media_profile_source_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(row)
        data["auto_record"] = bool(data.get("auto_record"))
        try:
            data["retention_days"] = int(data.get("retention_days") if data.get("retention_days") is not None else 30)
        except (TypeError, ValueError):
            data["retention_days"] = 30
        return data

    async def get_media_profile(self, username: str) -> Optional[Dict[str, Any]]:
        """Récupère la fiche locale d'un profil média."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM media_profiles WHERE username = ? LIMIT 1",
                (username,),
            )
            row = await cursor.fetchone()
            return self._format_media_profile_row(dict(row)) if row else None

    async def get_all_media_profiles(self) -> List[Dict[str, Any]]:
        """Récupère toutes les fiches locales média."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM media_profiles ORDER BY username")
            rows = await cursor.fetchall()
            return [self._format_media_profile_row(dict(row)) for row in rows]

    async def upsert_media_profile(self, username: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Crée ou met à jour les informations enrichies d'un profil média."""
        await self.initialize()

        now = int(datetime.now().timestamp())
        age = data.get("age")
        try:
            age = int(age) if age not in (None, "") else None
        except (TypeError, ValueError):
            age = None

        payload = {
            "display_name": data.get("display_name"),
            "first_name": data.get("first_name"),
            "last_name": data.get("last_name"),
            "age": age,
            "birth_date": data.get("birth_date"),
            "profile_image_url": data.get("profile_image_url"),
            "profile_image_source_url": data.get("profile_image_source_url"),
            "profile_image_path": data.get("profile_image_path"),
            "address": data.get("address"),
            "city": data.get("city"),
            "region": data.get("region"),
            "postal_code": data.get("postal_code"),
            "country": data.get("country"),
            "aliases": data.get("aliases"),
            "tags": data.get("tags"),
            "notes": data.get("notes"),
            "social_urls": self._json_list(data.get("social_urls")),
            "stream_urls": self._json_list(data.get("stream_urls")),
            "profile_urls": self._json_list(data.get("profile_urls")),
        }

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO media_profiles (
                    username, display_name, first_name, last_name, age,
                    birth_date, profile_image_url, profile_image_source_url, profile_image_path,
                    address, city, region, postal_code, country,
                    aliases, tags, notes, social_urls, stream_urls, profile_urls,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    display_name = ?,
                    first_name = ?,
                    last_name = ?,
                    age = ?,
                    birth_date = ?,
                    profile_image_url = ?,
                    profile_image_source_url = ?,
                    profile_image_path = ?,
                    address = ?,
                    city = ?,
                    region = ?,
                    postal_code = ?,
                    country = ?,
                    aliases = ?,
                    tags = ?,
                    notes = ?,
                    social_urls = ?,
                    stream_urls = ?,
                    profile_urls = ?,
                    updated_at = ?
            """, (
                username,
                payload["display_name"],
                payload["first_name"],
                payload["last_name"],
                payload["age"],
                payload["birth_date"],
                payload["profile_image_url"],
                payload["profile_image_source_url"],
                payload["profile_image_path"],
                payload["address"],
                payload["city"],
                payload["region"],
                payload["postal_code"],
                payload["country"],
                payload["aliases"],
                payload["tags"],
                payload["notes"],
                payload["social_urls"],
                payload["stream_urls"],
                payload["profile_urls"],
                now,
                now,
                payload["display_name"],
                payload["first_name"],
                payload["last_name"],
                payload["age"],
                payload["birth_date"],
                payload["profile_image_url"],
                payload["profile_image_source_url"],
                payload["profile_image_path"],
                payload["address"],
                payload["city"],
                payload["region"],
                payload["postal_code"],
                payload["country"],
                payload["aliases"],
                payload["tags"],
                payload["notes"],
                payload["social_urls"],
                payload["stream_urls"],
                payload["profile_urls"],
                now,
            ))
            await db.commit()

        profile = await self.get_media_profile(username)
        return profile or {"username": username}

    async def rename_media_profile(self, old_username: str, new_username: str) -> bool:
        """Rename local media-profile metadata without merging conflicting rows."""
        await self.initialize()
        old_username = str(old_username or "").strip()
        new_username = str(new_username or "").strip()
        if not old_username or not new_username or old_username == new_username:
            return False

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT 1 FROM media_profiles WHERE username = ? LIMIT 1",
                (old_username,),
            )
            if not await cursor.fetchone():
                return False
            cursor = await db.execute(
                "SELECT 1 FROM media_profiles WHERE username = ? LIMIT 1",
                (new_username,),
            )
            if await cursor.fetchone():
                return False
            now = int(datetime.now().timestamp())
            await db.execute(
                "UPDATE media_profiles SET username = ?, updated_at = ? WHERE username = ?",
                (new_username, now, old_username),
            )
            await db.execute(
                "UPDATE media_profile_sources SET profile_username = ? WHERE profile_username = ?",
                (new_username, old_username),
            )
            await db.commit()
            return True

    async def get_media_profile_sources(self, profile_username: str) -> List[Dict[str, Any]]:
        """Liste les sources d'enregistrement liées à un profil média."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM media_profile_sources
                WHERE profile_username = ?
                ORDER BY source_type, channel_username
                """,
                (profile_username,),
            )
            rows = await cursor.fetchall()
            return [self._format_media_profile_source_row(dict(row)) for row in rows]

    async def get_all_media_profile_sources(self) -> List[Dict[str, Any]]:
        """Liste toutes les sources d'enregistrement Media."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM media_profile_sources
                ORDER BY profile_username, source_type, channel_username
                """
            )
            rows = await cursor.fetchall()
            return [self._format_media_profile_source_row(dict(row)) for row in rows]

    async def get_media_profile_sources_for_auto_record(self) -> List[Dict[str, Any]]:
        """Liste les sources Media dont l'enregistrement automatique est actif."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM media_profile_sources
                WHERE auto_record = 1
                ORDER BY profile_username, source_type, channel_username
                """
            )
            rows = await cursor.fetchall()
            return [self._format_media_profile_source_row(dict(row)) for row in rows]

    async def upsert_media_profile_source(
        self,
        profile_username: str,
        source_type: str,
        channel_username: str,
        channel_url: Optional[str] = None,
        auto_record: bool = True,
        record_quality: str = "best",
        retention_days: int = 30,
        record_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Crée ou met à jour une source de stream pour un profil média."""
        await self.initialize()

        now = int(datetime.now().timestamp())
        source_type = self._normalize_source_type(source_type)
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO media_profile_sources (
                    profile_username, source_type, channel_username, channel_url,
                    auto_record, record_quality, retention_days, record_path,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_username, source_type, channel_username) DO UPDATE SET
                    channel_url = excluded.channel_url,
                    auto_record = excluded.auto_record,
                    record_quality = excluded.record_quality,
                    retention_days = excluded.retention_days,
                    record_path = excluded.record_path,
                    updated_at = excluded.updated_at
                """,
                (
                    profile_username,
                    source_type,
                    channel_username,
                    channel_url,
                    int(bool(auto_record)),
                    record_quality,
                    int(retention_days),
                    record_path,
                    now,
                    now,
                ),
            )
            await db.commit()

        sources = await self.get_media_profile_sources(profile_username)
        for source in sources:
            if (
                source.get("source_type") == source_type
                and source.get("channel_username") == channel_username
            ):
                return source
        return {
            "profile_username": profile_username,
            "source_type": source_type,
            "channel_username": channel_username,
            "channel_url": channel_url,
            "auto_record": bool(auto_record),
            "record_quality": record_quality,
            "retention_days": int(retention_days),
            "record_path": record_path,
        }

    async def replace_media_profile_sources(
        self,
        profile_username: str,
        sources: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Remplace toutes les sources d'un profil média."""
        await self.initialize()

        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM media_profile_sources WHERE profile_username = ?",
                (profile_username,),
            )
            for source in sources:
                await db.execute(
                    """
                    INSERT INTO media_profile_sources (
                        profile_username, source_type, channel_username, channel_url,
                        auto_record, record_quality, retention_days, record_path,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_username,
                        self._normalize_source_type(source.get("source_type")),
                        source.get("channel_username"),
                        source.get("channel_url"),
                        int(bool(source.get("auto_record"))),
                        source.get("record_quality") or "best",
                        int(source.get("retention_days") if source.get("retention_days") is not None else 30),
                        source.get("record_path"),
                        now,
                        now,
                    ),
                )
            await db.commit()

        return await self.get_media_profile_sources(profile_username)

    async def delete_media_profile_sources(self, profile_username: str) -> int:
        """Supprime les sources de stream liées à un profil média."""
        await self.initialize()

        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM media_profile_sources WHERE profile_username = ?",
                (profile_username,),
            )
            await db.commit()
            return cursor.rowcount or 0

    async def delete_media_profile(self, username: str) -> None:
        """Supprime la fiche locale d'un profil média."""
        await self.initialize()

        async with self._connect() as db:
            await db.execute("DELETE FROM media_profile_sources WHERE profile_username = ?", (username,))
            await db.execute("DELETE FROM media_profiles WHERE username = ?", (username,))
            await db.commit()

    async def delete_recordings_for_username(self, username: str) -> int:
        """Supprime tous les enregistrements DB associés à un profil."""
        await self.initialize()

        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM recordings WHERE username = ?", (username,))
            await db.commit()
            return cursor.rowcount or 0

    async def update_all_models_retention_days(self, retention_days: int) -> int:
        """Apply one retention window to every tracked model."""
        await self.initialize()

        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            cursor = await db.execute("""
                UPDATE models
                SET retention_days = ?, updated_at = ?
            """, (retention_days, now))
            await db.commit()
            return cursor.rowcount or 0
    
    async def add_or_update_recording(
        self,
        username: str,
        filename: str,
        file_path: str,
        file_size: int,
        recording_id: Optional[str] = None,
        duration_seconds: int = 0,
        thumbnail_path: Optional[str] = None,
        mp4_path: Optional[str] = None,
        mp4_size: Optional[int] = None,
        is_converted: bool = False,
        media_kind: Optional[str] = None,
        title: Optional[str] = None,
        import_status: Optional[str] = None,
        import_error: Optional[str] = None,
        source_mtime: Optional[int] = None,
        playable_path: Optional[str] = None,
        playable_size: Optional[int] = None,
        protected_from_retention: Optional[bool] = None,
        created_at: Optional[int] = None,
        replace_media_paths: bool = False,
    ):
        """Ajoute ou met à jour un enregistrement"""
        await self.initialize()
        
        now = int(datetime.now().timestamp())
        created_at_update = created_at
        created_at = created_at or now
        media_kind = (media_kind or "recording").strip().lower() or "recording"
        protected_value = 1 if protected_from_retention else 0
        
        # Générer recording_id si non fourni
        if not recording_id:
            recording_id = f"{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        async with self._connect() as db:
            await db.execute("""
                INSERT INTO recordings (
                    username, recording_id, filename, file_path, file_size, 
                    duration_seconds, thumbnail_path, mp4_path, mp4_size, is_converted,
                    media_kind, title, import_status, import_error, source_mtime,
                    playable_path, playable_size, protected_from_retention, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, filename) DO UPDATE SET
                    file_path = ?,
                    file_size = ?,
                    duration_seconds = ?,
                    thumbnail_path = CASE WHEN ? THEN ? ELSE COALESCE(?, thumbnail_path) END,
                    mp4_path = CASE WHEN ? THEN ? ELSE COALESCE(?, mp4_path) END,
                    mp4_size = CASE WHEN ? THEN ? ELSE COALESCE(?, mp4_size) END,
                    is_converted = ?,
                    media_kind = COALESCE(?, media_kind),
                    title = COALESCE(?, title),
                    import_status = COALESCE(?, import_status),
                    import_error = ?,
                    source_mtime = COALESCE(?, source_mtime),
                    playable_path = CASE WHEN ? THEN ? ELSE COALESCE(?, playable_path) END,
                    playable_size = CASE WHEN ? THEN ? ELSE COALESCE(?, playable_size) END,
                    protected_from_retention = ?,
                    created_at = COALESCE(?, created_at)
            """, (
                username, recording_id, filename, file_path, file_size,
                duration_seconds, thumbnail_path, mp4_path, mp4_size, is_converted,
                media_kind, title, import_status, import_error, source_mtime,
                playable_path, playable_size, protected_value, created_at,
                file_path, file_size, duration_seconds,
                replace_media_paths, thumbnail_path, thumbnail_path,
                replace_media_paths, mp4_path, mp4_path,
                replace_media_paths, mp4_size, mp4_size,
                is_converted,
                media_kind, title, import_status, import_error, source_mtime,
                replace_media_paths, playable_path, playable_path,
                replace_media_paths, playable_size, playable_size,
                protected_value, created_at_update
            ))
            await db.commit()
    
    async def get_recordings(self, username: str) -> List[Dict[str, Any]]:
        """Récupère les enregistrements d'un modèle"""
        await self.initialize()
        
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM recordings 
                WHERE username = ? 
                ORDER BY created_at DESC
                """,
                (username,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_recordings_for_usernames(self, usernames: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Récupère les enregistrements de plusieurs profils en lots."""
        await self.initialize()

        clean_usernames = sorted({str(username) for username in usernames if str(username or "").strip()})
        grouped: Dict[str, List[Dict[str, Any]]] = {username: [] for username in clean_usernames}
        if not clean_usernames:
            return grouped

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            for start in range(0, len(clean_usernames), 500):
                chunk = clean_usernames[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                cursor = await db.execute(
                    f"""
                    SELECT * FROM recordings
                    WHERE username IN ({placeholders})
                    ORDER BY username, created_at DESC
                    """,
                    chunk,
                )
                rows = await cursor.fetchall()
                for row in rows:
                    data = dict(row)
                    grouped.setdefault(data.get("username"), []).append(data)
        return grouped

    async def get_import_recordings(self) -> List[Dict[str, Any]]:
        """Récupère les médias importés."""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM recordings
                WHERE media_kind = 'import'
                ORDER BY created_at DESC
                """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def get_recordings_count(self, username: str) -> int:
        """Compte les enregistrements (convertis ou non)"""
        await self.initialize()

        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM recordings WHERE username = ?",
                (username,)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0
    
    async def delete_recording(self, username: str, filename: str):
        """Supprime un enregistrement de la base de données"""
        await self.initialize()

        async with self._connect() as db:
            await db.execute(
                "DELETE FROM recordings WHERE username = ? AND filename = ?",
                (username, filename)
            )
            await db.commit()

    async def delete_recording_by_id(self, recording_id: str):
        """Supprime un enregistrement de la base par son ID stable."""
        await self.initialize()

        async with self._connect() as db:
            await db.execute(
                "DELETE FROM recordings WHERE recording_id = ?",
                (recording_id,)
            )
            await db.commit()

    async def delete_playback_position(self, recording_id: str):
        """Supprime la position de lecture associée à un média."""
        await self.initialize()

        async with self._connect() as db:
            await db.execute(
                "DELETE FROM playback_positions WHERE recording_id = ?",
                (recording_id,)
            )
            await db.commit()

    async def mark_conversion_failed(self, username: str, filename: str, error: str):
        """Incrémente le compteur d'échecs et stocke l'erreur pour un enregistrement."""
        await self.initialize()
        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE recordings
                SET conversion_attempts = COALESCE(conversion_attempts, 0) + 1,
                    conversion_error = ?,
                    last_conversion_attempt = ?
                WHERE username = ? AND filename = ?
                """,
                (error[:500], now, username, filename)
            )
            await db.commit()

    async def reset_conversion_failure(self, recording_id: str) -> bool:
        """Réinitialise le compteur d'échecs (pour retry manuel). Retourne True si trouvé."""
        await self.initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE recordings
                SET conversion_attempts = 0,
                    conversion_error = NULL,
                    last_conversion_attempt = NULL
                WHERE recording_id = ?
                """,
                (recording_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_recording_by_id(self, recording_id: str) -> Optional[Dict[str, Any]]:
        """Récupère un enregistrement par son recording_id."""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM recordings WHERE recording_id = ? LIMIT 1",
                (recording_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    # ==========================================
    # Chaturbate Auth CRUD
    # ==========================================

    async def save_auth_state(
        self,
        username: str,
        password_hash: str,
        is_logged_in: bool = False,
        session_cookies: Optional[str] = None,
        cf_clearance: Optional[str] = None,
        csrf_token: Optional[str] = None,
        last_login_at: Optional[int] = None,
        last_error: Optional[str] = None
    ):
        """Save or update Chaturbate auth state"""
        await self.initialize()
        now = int(datetime.now().timestamp())

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO chaturbate_auth (
                    id, username, password_hash, is_logged_in,
                    session_cookies, cf_clearance, csrf_token,
                    last_login_at, last_error, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    username = ?,
                    password_hash = ?,
                    is_logged_in = ?,
                    session_cookies = COALESCE(?, session_cookies),
                    cf_clearance = COALESCE(?, cf_clearance),
                    csrf_token = COALESCE(?, csrf_token),
                    last_login_at = COALESCE(?, last_login_at),
                    last_error = ?,
                    updated_at = ?
            """, (
                username, password_hash, is_logged_in,
                session_cookies, cf_clearance, csrf_token,
                last_login_at, last_error, now,
                username, password_hash, is_logged_in,
                session_cookies, cf_clearance, csrf_token,
                last_login_at, last_error, now
            ))
            await db.commit()

    async def get_auth_state(self) -> Optional[Dict[str, Any]]:
        """Get Chaturbate auth state"""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM chaturbate_auth WHERE id = 1"
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
        return None

    async def clear_auth_state(self):
        """Clear Chaturbate auth state"""
        await self.initialize()

        async with self._connect() as db:
            await db.execute("DELETE FROM chaturbate_auth WHERE id = 1")
            await db.commit()

    # ==========================================
    # Generic Provider Sessions CRUD
    # ==========================================

    async def save_provider_session(
        self,
        source_type: str,
        username: Optional[str] = None,
        is_logged_in: bool = False,
        session_cookies: Optional[str] = None,
        local_storage: Optional[str] = None,
        last_login_at: Optional[int] = None,
        last_error: Optional[str] = None,
    ) -> None:
        await self.initialize()
        source_type = (source_type or "").strip().lower()
        if not source_type:
            raise ValueError("source_type is required")
        now = int(datetime.now().timestamp())
        last_login_at = last_login_at if last_login_at is not None else (now if is_logged_in else None)

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO provider_sessions (
                    source_type, username, is_logged_in, session_cookies,
                    local_storage, last_login_at, last_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type) DO UPDATE SET
                    username = COALESCE(?, username),
                    is_logged_in = ?,
                    session_cookies = COALESCE(?, session_cookies),
                    local_storage = COALESCE(?, local_storage),
                    last_login_at = ?,
                    last_error = ?,
                    updated_at = ?
            """, (
                source_type, username, is_logged_in, session_cookies,
                local_storage, last_login_at, last_error, now,
                username, is_logged_in, session_cookies, local_storage,
                last_login_at, last_error, now,
            ))
            await db.commit()

    async def save_provider_credentials(
        self,
        source_type: str,
        username: str,
        password: str,
    ) -> None:
        await self.initialize()
        source_type = (source_type or "").strip().lower()
        username = (username or "").strip()
        if not source_type:
            raise ValueError("source_type is required")
        if not username:
            raise ValueError("username is required")
        now = int(datetime.now().timestamp())

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO provider_sessions (
                    source_type, username, credential_username,
                    credential_password, credentials_updated_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type) DO UPDATE SET
                    username = ?,
                    credential_username = ?,
                    credential_password = ?,
                    credentials_updated_at = ?,
                    updated_at = ?
            """, (
                source_type, username, username, password, now, now,
                username, username, password, now, now,
            ))
            await db.commit()

    async def get_provider_session(self, source_type: str) -> Optional[Dict[str, Any]]:
        await self.initialize()
        source_type = (source_type or "").strip().lower()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM provider_sessions WHERE source_type = ?",
                (source_type,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def clear_provider_session(self, source_type: str) -> None:
        await self.initialize()
        source_type = (source_type or "").strip().lower()
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM provider_sessions WHERE source_type = ?",
                (source_type,),
            )
            await db.commit()

    # ==========================================
    # Followed Models CRUD
    # ==========================================

    async def upsert_followed_model(
        self,
        username: str,
        display_name: Optional[str] = None,
        is_online: bool = False,
        viewers: int = 0,
        thumbnail_url: Optional[str] = None,
        source_type: str = "chaturbate",
        room_status: Optional[str] = None,
    ):
        """Add or update a followed model"""
        await self.initialize()
        now = int(datetime.now().timestamp())
        source_type = self._normalize_source_type(source_type)

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO followed_models (
                    username, display_name, is_online, viewers,
                    thumbnail_url, last_seen_online_at, synced_at, source_type,
                    room_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, source_type) DO UPDATE SET
                    display_name = COALESCE(?, display_name),
                    is_online = ?,
                    viewers = ?,
                    thumbnail_url = COALESCE(?, thumbnail_url),
                    last_seen_online_at = CASE WHEN ? THEN ? ELSE last_seen_online_at END,
                    synced_at = ?,
                    room_status = ?
            """, (
                username, display_name, is_online, viewers,
                thumbnail_url, now if is_online else None, now, source_type,
                room_status,
                display_name, is_online, viewers, thumbnail_url,
                is_online, now, now, room_status,
            ))
            await db.commit()

    async def get_all_followed(self) -> List[Dict[str, Any]]:
        """Get all followed models"""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM followed_models ORDER BY is_online DESC, username"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_followed_model(
        self, username: str, source_type: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Récupère un followed_model par username et source_type optionnel.
        Utilisé pour résoudre la plateforme d'un modèle qui n'est pas dans
        `tracked_models` mais dans la liste des favoris."""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if source_type:
                cursor = await db.execute(
                    "SELECT * FROM followed_models WHERE username = ? AND source_type = ? LIMIT 1",
                    (username, self._normalize_source_type(source_type)),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM followed_models
                    WHERE username = ?
                    ORDER BY CASE WHEN source_type = 'chaturbate' THEN 0 ELSE 1 END, source_type
                    LIMIT 1
                    """,
                    (username,),
                )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def clear_followed(self):
        """Clear all followed models"""
        await self.initialize()

        async with self._connect() as db:
            await db.execute("DELETE FROM followed_models")
            await db.commit()

    async def delete_followed_model(
        self, username: str, source_type: Optional[str] = None
    ) -> None:
        """Supprime un followed_model par username (et optionnellement par
        source_type). Utilisé après unfollow depuis la page watch."""
        await self.initialize()
        async with self._connect() as db:
            if source_type:
                await db.execute(
                    "DELETE FROM followed_models WHERE username = ? AND source_type = ?",
                    (username, self._normalize_source_type(source_type)),
                )
            else:
                await db.execute(
                    "DELETE FROM followed_models WHERE username = ?",
                    (username,),
                )
            await db.commit()

    async def remove_unfollowed(
        self, current_usernames: set, source_type: str = "chaturbate"
    ):
        """Remove followed models no longer in the followed list (scoped par
        source_type pour ne pas toucher les autres sources)."""
        await self.initialize()
        source_type = self._normalize_source_type(source_type)

        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT username FROM followed_models WHERE source_type = ?",
                (source_type,),
            )
            rows = await cursor.fetchall()
            for row in rows:
                if row[0] not in current_usernames:
                    await db.execute(
                        "DELETE FROM followed_models WHERE username = ? AND source_type = ?",
                        (row[0], source_type),
                    )
            await db.commit()

    async def get_all_recordings_paginated(
        self,
        page: int = 1,
        limit: int = 20,
        username_filter: Optional[str] = None,
        show_ts: bool = False
    ) -> Dict[str, Any]:
        """Get all recordings with pagination"""
        await self.initialize()

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row

            # Raw transport streams are opt-in; directly playable WebM/MP4
            # captures and imported media remain visible by default.
            where_clauses = ["1=1"]
            where_params = []
            if not show_ts:
                where_clauses.append(
                    "(media_kind = 'import' OR is_converted = 1 OR mp4_path IS NOT NULL "
                    "OR LOWER(file_path) NOT LIKE '%.ts')"
                )
            if username_filter:
                where_clauses.append("username = ?")
                where_params.append(username_filter)

            where_sql = " AND ".join(where_clauses)

            # Count total
            count_sql = f"SELECT COUNT(*) FROM recordings WHERE {where_sql}"
            cursor = await db.execute(count_sql, where_params)
            row = await cursor.fetchone()
            total = row[0] if row else 0

            # Fetch page
            offset = (page - 1) * limit
            query_sql = f"SELECT * FROM recordings WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"
            query_params = list(where_params) + [limit, offset]

            cursor = await db.execute(query_sql, query_params)
            rows = await cursor.fetchall()

            # Total size - respects show_ts filter
            if show_ts:
                size_sql = f"SELECT COALESCE(SUM(COALESCE(playable_size, mp4_size, file_size)), 0) FROM recordings WHERE {where_sql}"
            else:
                # The WHERE clause already excludes raw TS rows.
                size_sql = f"SELECT COALESCE(SUM(COALESCE(playable_size, mp4_size, file_size)), 0) FROM recordings WHERE {where_sql}"

            cursor = await db.execute(size_sql, where_params)
            size_row = await cursor.fetchone()
            total_size = size_row[0] if size_row else 0

            return {
                "recordings": [dict(row) for row in rows],
                "total": total,
                "total_size": total_size,
                "page": page,
                "limit": limit,
                "total_pages": max(1, (total + limit - 1) // limit)
            }

    async def get_distinct_recording_usernames(self) -> List[str]:
        """Get list of usernames that have recordings"""
        await self.initialize()

        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT DISTINCT username FROM recordings ORDER BY username"
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    # ==========================================
    # Settings CRUD
    # ==========================================

    async def get_setting(self, key: str) -> Optional[str]:
        """Get a setting value by key"""
        await self.initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_setting(self, key: str, value: str):
        """Set a setting value"""
        await self.initialize()
        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            await db.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = ?, updated_at = ?
            """, (key, value, now, value, now))
            await db.commit()

    def _model_volume_key(self, username: str) -> str:
        """Stable settings key for a profile's playback volume."""
        normalized = (username or "").strip().lower()
        return f"model_volume:{normalized}"

    async def get_model_volume(self, username: str) -> Optional[float]:
        """Get the saved playback volume for a profile, if one exists."""
        normalized = (username or "").strip()
        if not normalized:
            return None

        value = await self.get_setting(self._model_volume_key(normalized))
        if value is None:
            return None

        try:
            volume = float(value)
        except (TypeError, ValueError):
            return None

        if 0 <= volume <= 1:
            return volume
        return None

    async def set_model_volume(self, username: str, volume: float):
        """Persist a profile's playback volume."""
        normalized = (username or "").strip()
        if not normalized:
            raise ValueError("username is required")
        if not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")

        await self.set_setting(self._model_volume_key(normalized), f"{volume:.4f}")

    async def get_blacklisted_tags(self) -> List[str]:
        """Get blacklisted tags list"""
        value = await self.get_setting("blacklisted_tags")
        if value:
            return json.loads(value)
        return []

    async def set_blacklisted_tags(self, tags: List[str]):
        """Set blacklisted tags list"""
        await self.set_setting("blacklisted_tags", json.dumps(tags))

    async def get_disabled_providers(self) -> List[str]:
        """Get provider source types hidden from Discover."""
        value = await self.get_setting("disabled_providers")
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        seen = set()
        providers = []
        for item in parsed:
            source_type = str(item or "").strip().lower()
            if not source_type or source_type in seen:
                continue
            seen.add(source_type)
            providers.append(source_type)
        return providers

    async def set_disabled_providers(self, providers: List[str]):
        """Set provider source types hidden from Discover."""
        normalized = sorted({
            str(source_type or "").strip().lower()
            for source_type in providers
            if str(source_type or "").strip()
        })
        await self.set_setting("disabled_providers", json.dumps(normalized))

    # ==========================================
    # Playback Positions CRUD
    # ==========================================

    async def get_playback_position(self, recording_id: str) -> Optional[Dict[str, Any]]:
        """Get playback position for a recording"""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM playback_positions WHERE recording_id = ?",
                (recording_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def save_playback_position(
        self,
        recording_id: str,
        username: str,
        position_seconds: float,
        duration_seconds: float = 0,
        mark_watched: bool = False,
    ):
        """Save playback position for a recording"""
        await self.initialize()
        now = int(datetime.now().timestamp())
        watched_at = now if mark_watched else None
        async with self._connect() as db:
            await db.execute("""
                INSERT INTO playback_positions (
                    recording_id, username, position_seconds, duration_seconds, watched_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(recording_id) DO UPDATE SET
                    position_seconds = ?,
                    duration_seconds = ?,
                    watched_at = CASE
                        WHEN ? THEN COALESCE(playback_positions.watched_at, ?)
                        ELSE playback_positions.watched_at
                    END,
                    updated_at = ?
            """, (
                recording_id, username, position_seconds, duration_seconds, watched_at, now,
                position_seconds, duration_seconds, 1 if mark_watched else 0, watched_at, now,
            ))
            await db.commit()

    async def get_all_playback_positions(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all playback positions, optionally filtered by username"""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if username:
                cursor = await db.execute(
                    "SELECT * FROM playback_positions WHERE username = ? ORDER BY updated_at DESC",
                    (username,)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM playback_positions ORDER BY updated_at DESC"
                )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_recordings_grouped_by_model(self, show_ts: bool = False) -> List[Dict[str, Any]]:
        """Get recordings grouped by model with stats"""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            # Raw TS rows are opt-in; non-TS browser captures remain visible.
            if show_ts:
                where_clause = ""
            else:
                where_clause = (
                    "WHERE media_kind = 'import' OR is_converted = 1 OR mp4_path IS NOT NULL "
                    "OR LOWER(file_path) NOT LIKE '%.ts'"
                )
            cursor = await db.execute(f"""
                SELECT
                    username,
                    COUNT(*) as recording_count,
                    COALESCE(SUM(COALESCE(playable_size, mp4_size, file_size)), 0) as total_size,
                    MAX(created_at) as last_recording_at,
                    COALESCE(SUM(duration_seconds), 0) as total_duration
                FROM recordings
                {where_clause}
                GROUP BY username
                ORDER BY last_recording_at DESC
            """)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==========================================
    # Plugins CRUD
    # ==========================================

    async def plugin_list_records(self) -> List[Dict[str, Any]]:
        """Liste tous les enregistrements plugins (installés ou en erreur)."""
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM plugins ORDER BY id"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def plugin_get_record(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM plugins WHERE id = ?", (plugin_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def plugin_upsert_record(
        self,
        plugin_id: str,
        version: str,
        source_type: str,
        source_repo: Optional[str],
        enabled: bool = True,
        installed: bool = True,
        status: str = "pending_restart",
        manifest_json: Optional[str] = None,
    ):
        await self.initialize()
        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO plugins (
                    id, version, source_type, source_repo, enabled, installed,
                    status, manifest_json, installed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    version = excluded.version,
                    source_type = excluded.source_type,
                    source_repo = excluded.source_repo,
                    enabled = excluded.enabled,
                    installed = excluded.installed,
                    status = excluded.status,
                    manifest_json = excluded.manifest_json,
                    updated_at = excluded.updated_at
                """,
                (
                    plugin_id, version, source_type, source_repo,
                    int(bool(enabled)), int(bool(installed)), status,
                    manifest_json, now, now,
                ),
            )
            await db.commit()

    async def plugin_set_enabled(self, plugin_id: str, enabled: bool):
        await self.initialize()
        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            await db.execute(
                "UPDATE plugins SET enabled = ?, status = ?, updated_at = ? WHERE id = ?",
                (int(bool(enabled)), "pending_restart", now, plugin_id),
            )
            await db.commit()

    async def plugin_set_status(
        self, plugin_id: str, status: str, error: Optional[str] = None
    ):
        await self.initialize()
        now = int(datetime.now().timestamp())
        async with self._connect() as db:
            await db.execute(
                "UPDATE plugins SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, error, now, plugin_id),
            )
            await db.commit()

    async def plugin_delete_record(self, plugin_id: str):
        await self.initialize()
        async with self._connect() as db:
            await db.execute("DELETE FROM plugins WHERE id = ?", (plugin_id,))
            await db.commit()

    # ==========================================
    # JSON Migration
    # ==========================================

    async def migrate_from_json(self, json_path: Path):
        """Migre les données depuis le fichier JSON vers SQLite"""
        if not json_path.exists():
            return
        
        await self.initialize()
        
        try:
            with open(json_path, 'r') as f:
                data = json.load(f)
                models = data.get('models', []) if isinstance(data, dict) else data
            
            for model in models:
                username = model.get('username')
                if username:
                    await self.add_or_update_model(
                        username=username,
                        auto_record=model.get('autoRecord', True),
                        record_quality=model.get('recordQuality', 'best'),
                        retention_days=model.get('retentionDays', 30),
                        source_type=model.get('sourceType') or model.get('source_type'),
                    )
            
            logger.info("Migration JSON vers SQLite terminée", models_count=len(models))
        
        except Exception as e:
            logger.error("Erreur lors de la migration JSON", error=str(e), exc_info=True)
