"""
Gestion de la base de données SQLite pour le cache des modèles
"""
import aiosqlite
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
from ..logger import logger

class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._initialized = False
    
    async def initialize(self):
        """Initialise la base de données et crée les tables"""
        if self._initialized:
            return
        
        async with aiosqlite.connect(self.db_path) as db:
            # Table pour les modèles et leur statut
            await db.execute("""
                CREATE TABLE IF NOT EXISTS models (
                    username TEXT PRIMARY KEY,
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
                    created_at INTEGER,
                    updated_at INTEGER
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
                    username TEXT PRIMARY KEY,
                    display_name TEXT,
                    is_online BOOLEAN DEFAULT 0,
                    viewers INTEGER DEFAULT 0,
                    thumbnail_url TEXT,
                    last_seen_online_at INTEGER,
                    synced_at INTEGER
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
                    updated_at INTEGER
                )
            """)

            # Index pour les requêtes fréquentes
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_models_online
                ON models(is_online, username)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_recordings_username
                ON recordings(username, created_at DESC)
            """)

            await db.commit()
            
        self._initialized = True
        logger.info("Base de données initialisée", db_path=str(self.db_path))
    
    async def add_or_update_model(
        self, 
        username: str,
        display_name: Optional[str] = None,
        auto_record: bool = True,
        record_quality: str = "best",
        retention_days: int = 30
    ):
        """Ajoute ou met à jour un modèle"""
        await self.initialize()
        
        now = int(datetime.now().timestamp())
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO models (
                    username, display_name, auto_record, record_quality, 
                    retention_days, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    display_name = COALESCE(?, display_name),
                    auto_record = ?,
                    record_quality = ?,
                    retention_days = ?,
                    updated_at = ?
            """, (
                username, display_name, auto_record, record_quality,
                retention_days, now, now,
                display_name, auto_record, record_quality, retention_days, now
            ))
            await db.commit()
        
        logger.debug("Modèle ajouté/mis à jour", username=username)
    
    async def update_model_status(
        self,
        username: str,
        is_online: bool,
        viewers: int = 0,
        is_recording: bool = False,
        thumbnail_path: Optional[str] = None
    ):
        """Met à jour le statut d'un modèle"""
        await self.initialize()
        
        now = int(datetime.now().timestamp())
        
        async with aiosqlite.connect(self.db_path) as db:
            update_fields = {
                'is_online': is_online,
                'viewers': viewers,
                'is_recording': is_recording,
                'last_check_at': now,
                'updated_at': now
            }
            
            if thumbnail_path:
                update_fields['thumbnail_path'] = thumbnail_path
                update_fields['thumbnail_updated_at'] = now
            
            placeholders = ', '.join(f"{k} = ?" for k in update_fields.keys())
            values = list(update_fields.values()) + [username]
            
            await db.execute(
                f"UPDATE models SET {placeholders} WHERE username = ?",
                values
            )
            await db.commit()
    
    async def get_model(self, username: str) -> Optional[Dict[str, Any]]:
        """Récupère les informations d'un modèle"""
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM models WHERE username = ?",
                (username,)
            )
            row = await cursor.fetchone()
            
            if row:
                return dict(row)
            return None
    
    async def get_all_models(self) -> List[Dict[str, Any]]:
        """Récupère tous les modèles"""
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM models ORDER BY username"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def get_models_for_auto_record(self) -> List[Dict[str, Any]]:
        """Récupère les modèles avec auto-record activé"""
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM models WHERE auto_record = 1 ORDER BY username"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def delete_model(self, username: str):
        """Supprime un modèle"""
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM models WHERE username = ?", (username,))
            await db.commit()
        
        logger.info("Modèle supprimé", username=username)
    
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
        is_converted: bool = False
    ):
        """Ajoute ou met à jour un enregistrement"""
        await self.initialize()
        
        now = int(datetime.now().timestamp())
        
        # Générer recording_id si non fourni
        if not recording_id:
            recording_id = f"{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO recordings (
                    username, recording_id, filename, file_path, file_size, 
                    duration_seconds, thumbnail_path, mp4_path, mp4_size, is_converted, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, filename) DO UPDATE SET
                    file_size = ?,
                    duration_seconds = ?,
                    thumbnail_path = COALESCE(?, thumbnail_path),
                    mp4_path = COALESCE(?, mp4_path),
                    mp4_size = COALESCE(?, mp4_size),
                    is_converted = ?
            """, (
                username, recording_id, filename, file_path, file_size,
                duration_seconds, thumbnail_path, mp4_path, mp4_size, is_converted, now,
                file_size, duration_seconds, thumbnail_path, mp4_path, mp4_size, is_converted
            ))
            await db.commit()
    
    async def get_recordings(self, username: str) -> List[Dict[str, Any]]:
        """Récupère les enregistrements d'un modèle"""
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
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
    
    async def get_recordings_count(self, username: str) -> int:
        """Compte les enregistrements (convertis ou non)"""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM recordings WHERE username = ?",
                (username,)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0
    
    async def delete_recording(self, username: str, filename: str):
        """Supprime un enregistrement de la base de données"""
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM recordings WHERE username = ? AND filename = ?",
                (username, filename)
            )
            await db.commit()
    
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

        async with aiosqlite.connect(self.db_path) as db:
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

        async with aiosqlite.connect(self.db_path) as db:
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

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM chaturbate_auth WHERE id = 1")
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
        thumbnail_url: Optional[str] = None
    ):
        """Add or update a followed model"""
        await self.initialize()
        now = int(datetime.now().timestamp())

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO followed_models (
                    username, display_name, is_online, viewers,
                    thumbnail_url, last_seen_online_at, synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    display_name = COALESCE(?, display_name),
                    is_online = ?,
                    viewers = ?,
                    thumbnail_url = COALESCE(?, thumbnail_url),
                    last_seen_online_at = CASE WHEN ? THEN ? ELSE last_seen_online_at END,
                    synced_at = ?
            """, (
                username, display_name, is_online, viewers,
                thumbnail_url, now if is_online else None, now,
                display_name, is_online, viewers, thumbnail_url,
                is_online, now, now
            ))
            await db.commit()

    async def get_all_followed(self) -> List[Dict[str, Any]]:
        """Get all followed models"""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM followed_models ORDER BY is_online DESC, username"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def clear_followed(self):
        """Clear all followed models"""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM followed_models")
            await db.commit()

    async def remove_unfollowed(self, current_usernames: set):
        """Remove followed models no longer in the followed list"""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT username FROM followed_models")
            rows = await cursor.fetchall()
            for row in rows:
                if row[0] not in current_usernames:
                    await db.execute(
                        "DELETE FROM followed_models WHERE username = ?",
                        (row[0],)
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

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Base filter: when show_ts=False, only count converted recordings
            where_clauses = ["1=1"]
            where_params = []
            if not show_ts:
                where_clauses.append("(is_converted = 1 OR mp4_path IS NOT NULL)")
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
                size_sql = f"SELECT COALESCE(SUM(COALESCE(mp4_size, file_size)), 0) FROM recordings WHERE {where_sql}"
            else:
                # When not showing TS, only sum MP4 sizes for converted, or file_size for those with mp4_path
                size_sql = f"SELECT COALESCE(SUM(COALESCE(mp4_size, file_size)), 0) FROM recordings WHERE {where_sql}"

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

        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_setting(self, key: str, value: str):
        """Set a setting value"""
        await self.initialize()
        now = int(datetime.now().timestamp())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = ?, updated_at = ?
            """, (key, value, now, value, now))
            await db.commit()

    async def get_blacklisted_tags(self) -> List[str]:
        """Get blacklisted tags list"""
        value = await self.get_setting("blacklisted_tags")
        if value:
            return json.loads(value)
        return []

    async def set_blacklisted_tags(self, tags: List[str]):
        """Set blacklisted tags list"""
        await self.set_setting("blacklisted_tags", json.dumps(tags))

    # ==========================================
    # Playback Positions CRUD
    # ==========================================

    async def get_playback_position(self, recording_id: str) -> Optional[Dict[str, Any]]:
        """Get playback position for a recording"""
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
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
        duration_seconds: float = 0
    ):
        """Save playback position for a recording"""
        await self.initialize()
        now = int(datetime.now().timestamp())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO playback_positions (recording_id, username, position_seconds, duration_seconds, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(recording_id) DO UPDATE SET
                    position_seconds = ?,
                    duration_seconds = ?,
                    updated_at = ?
            """, (recording_id, username, position_seconds, duration_seconds, now,
                  position_seconds, duration_seconds, now))
            await db.commit()

    async def get_all_playback_positions(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all playback positions, optionally filtered by username"""
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # When show_ts is False, only count recordings that have been converted (have mp4_path)
            # or show all recordings when show_ts is True
            if show_ts:
                where_clause = ""
            else:
                where_clause = "WHERE is_converted = 1 OR mp4_path IS NOT NULL"
            cursor = await db.execute(f"""
                SELECT
                    username,
                    COUNT(*) as recording_count,
                    COALESCE(SUM(COALESCE(mp4_size, file_size)), 0) as total_size,
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
                        retention_days=model.get('retentionDays', 30)
                    )
            
            logger.info("Migration JSON vers SQLite terminée", models_count=len(models))
        
        except Exception as e:
            logger.error("Erreur lors de la migration JSON", error=str(e), exc_info=True)
