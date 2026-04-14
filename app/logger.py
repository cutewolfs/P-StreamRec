"""
Système de logging centralisé pour P-StreamRec
Fournit des logs détaillés, structurés et colorisés pour faciliter le debugging
"""

import logging
import sys
import collections
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict
import json

# Codes couleur ANSI
class Colors:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    
    # Couleurs de base
    BLACK = '\033[30m'
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    BLUE = '\033[34m'
    MAGENTA = '\033[35m'
    CYAN = '\033[36m'
    WHITE = '\033[37m'
    
    # Couleurs brillantes
    BRIGHT_RED = '\033[91m'
    BRIGHT_GREEN = '\033[92m'
    BRIGHT_YELLOW = '\033[93m'
    BRIGHT_BLUE = '\033[94m'
    BRIGHT_MAGENTA = '\033[95m'
    BRIGHT_CYAN = '\033[96m'
    
    # Backgrounds
    BG_RED = '\033[41m'
    BG_GREEN = '\033[42m'
    BG_YELLOW = '\033[43m'
    BG_BLUE = '\033[44m'


class DetailedFormatter(logging.Formatter):
    """Formatter personnalisé avec couleurs et emojis"""
    
    EMOJI_MAP = {
        'DEBUG': '🔍',
        'INFO': 'ℹ️',
        'WARNING': '⚠️',
        'ERROR': '❌',
        'CRITICAL': '🔥'
    }
    
    COLOR_MAP = {
        'DEBUG': Colors.CYAN,
        'INFO': Colors.GREEN,
        'WARNING': Colors.YELLOW,
        'ERROR': Colors.RED,
        'CRITICAL': Colors.BRIGHT_RED + Colors.BOLD
    }
    
    def format(self, record):
        # Timestamp
        timestamp = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        
        # Emoji et couleur selon le niveau
        emoji = self.EMOJI_MAP.get(record.levelname, '•')
        color = self.COLOR_MAP.get(record.levelname, Colors.RESET)
        
        # Nom du module
        module = record.name.split('.')[-1]
        
        # Message de base
        message = record.getMessage()
        
        # Construire le log
        log_line = f"{Colors.BRIGHT_BLUE}{timestamp}{Colors.RESET} {emoji} {color}[{record.levelname:8}]{Colors.RESET} {Colors.MAGENTA}{module:15}{Colors.RESET} │ {message}"
        
        # Ajouter les infos supplémentaires si disponibles
        if hasattr(record, 'extra_data') and record.extra_data:
            log_line += f"\n{Colors.CYAN}{'  ' * 10}└─ {json.dumps(record.extra_data, indent=2, ensure_ascii=False)}{Colors.RESET}"
        
        # Ajouter l'exception si présente
        if record.exc_info:
            log_line += f"\n{self.formatException(record.exc_info)}"
        
        return log_line


class MemoryLogHandler(logging.Handler):
    """Handler qui stocke les logs en mémoire dans un deque circulaire"""

    def __init__(self, max_entries: int = 2000):
        super().__init__()
        self.logs: collections.deque = collections.deque(maxlen=max_entries)

    def emit(self, record):
        # Strip ANSI color codes for stored logs
        message = record.getMessage()
        for code in ['\033[0m', '\033[1m', '\033[30m', '\033[31m', '\033[32m',
                     '\033[33m', '\033[34m', '\033[35m', '\033[36m', '\033[37m',
                     '\033[91m', '\033[92m', '\033[93m', '\033[94m', '\033[95m',
                     '\033[96m', '\033[41m', '\033[42m', '\033[43m', '\033[44m']:
            message = message.replace(code, '')

        entry = {
            "timestamp": datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
            "level": record.levelname,
            "module": record.name.split('.')[-1],
            "message": message,
        }
        if hasattr(record, 'extra_data') and record.extra_data:
            entry["extra"] = record.extra_data
        self.logs.append(entry)

    def get_logs(self, level: Optional[str] = None, limit: int = 200, offset: int = 0) -> List[Dict]:
        """Récupère les logs filtrés"""
        logs = list(self.logs)
        if level:
            level_upper = level.upper()
            logs = [l for l in logs if l["level"] == level_upper]
        # Return most recent first
        logs.reverse()
        return logs[offset:offset + limit]

    def get_total(self, level: Optional[str] = None) -> int:
        if level:
            return sum(1 for l in self.logs if l["level"] == level.upper())
        return len(self.logs)


class AppLogger:
    """Logger principal de l'application"""

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # Configuration du logger principal
        self.logger = logging.getLogger('p-streamrec')
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        # Handler console avec couleurs
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(DetailedFormatter())

        # Handler mémoire pour l'API
        self.memory_handler = MemoryLogHandler(max_entries=2000)
        self.memory_handler.setLevel(logging.DEBUG)

        # Nettoyer les handlers existants
        self.logger.handlers.clear()
        self.logger.addHandler(console_handler)
        self.logger.addHandler(self.memory_handler)

        self._initialized = True

        # Log de démarrage
        self.startup()
    
    def startup(self):
        """Log de démarrage de l'application"""
        self.logger.info("=" * 80)
        self.logger.info(f"{Colors.BRIGHT_CYAN}{Colors.BOLD}P-STREAMREC - Démarrage de l'application{Colors.RESET}")
        self.logger.info("=" * 80)
    
    def get_logger(self, name: str):
        """Obtenir un logger pour un module spécifique"""
        return logging.getLogger(f'p-streamrec.{name}')
    
    def debug(self, message: str, **extra):
        """Log DEBUG avec données supplémentaires"""
        self.logger.debug(message, extra={'extra_data': extra} if extra else {})
    
    def info(self, message: str, **extra):
        """Log INFO avec données supplémentaires"""
        self.logger.info(message, extra={'extra_data': extra} if extra else {})
    
    def warning(self, message: str, **extra):
        """Log WARNING avec données supplémentaires"""
        self.logger.warning(message, extra={'extra_data': extra} if extra else {})
    
    def error(self, message: str, exc_info: bool = False, **extra):
        """Log ERROR avec données supplémentaires"""
        self.logger.error(message, exc_info=exc_info, extra={'extra_data': extra} if extra else {})
    
    def critical(self, message: str, exc_info: bool = False, **extra):
        """Log CRITICAL avec données supplémentaires"""
        self.logger.critical(message, exc_info=exc_info, extra={'extra_data': extra} if extra else {})
    
    def section(self, title: str, char: str = '='):
        """Afficher une section"""
        line = char * 80
        self.logger.info(line)
        self.logger.info(f"{Colors.BOLD}{title}{Colors.RESET}")
        self.logger.info(line)
    
    def subsection(self, title: str):
        """Afficher une sous-section"""
        self.logger.info(f"\n{Colors.BRIGHT_CYAN}{'─' * 40}{Colors.RESET}")
        self.logger.info(f"{Colors.BRIGHT_CYAN}{title}{Colors.RESET}")
        self.logger.info(f"{Colors.BRIGHT_CYAN}{'─' * 40}{Colors.RESET}")
    
    def success(self, message: str, **extra):
        """Log de succès (vert)"""
        self.logger.info(f"{Colors.BRIGHT_GREEN}{message}{Colors.RESET}", extra={'extra_data': extra} if extra else {})
    
    def failure(self, message: str, **extra):
        """Log d'échec (rouge)"""
        self.logger.error(f"{Colors.BRIGHT_RED}{message}{Colors.RESET}", extra={'extra_data': extra} if extra else {})
    
    def progress(self, message: str, **extra):
        """Log de progression"""
        self.logger.info(f"{Colors.BRIGHT_YELLOW}{message}{Colors.RESET}", extra={'extra_data': extra} if extra else {})
    
    def api_request(self, method: str, path: str, **extra):
        """Log d'une requête API"""
        self.logger.info(f"{Colors.BRIGHT_BLUE}{method:6} {path}{Colors.RESET}", extra={'extra_data': extra} if extra else {})
    
    def api_response(self, status: int, path: str, duration_ms: Optional[float] = None, **extra):
        """Log d'une réponse API"""
        color = Colors.BRIGHT_GREEN if status < 400 else Colors.BRIGHT_RED
        duration_str = f" ({duration_ms:.2f}ms)" if duration_ms else ""
        self.logger.info(f"{color}[{status}] {path}{duration_str}{Colors.RESET}", extra={'extra_data': extra} if extra else {})
    
    def ffmpeg_start(self, session_id: str, person: str, url: str):
        """Log démarrage FFmpeg"""
        self.section(f"DÉMARRAGE ENREGISTREMENT - {person}")
        self.logger.info(f"Session ID: {session_id}")
        self.logger.info(f"Personne: {person}")
        self.logger.info(f"URL: {url[:80]}...")
    
    def ffmpeg_stop(self, session_id: str, person: str, duration: Optional[float] = None):
        """Log arrêt FFmpeg"""
        duration_str = f" ({duration:.1f}s)" if duration else ""
        self.logger.info(f"ARRÊT ENREGISTREMENT - {person}{duration_str}")
        self.logger.info(f"Session ID: {session_id}")
    
    def ffmpeg_error(self, session_id: str, error: str):
        """Log erreur FFmpeg"""
        self.logger.error(f"ERREUR FFMPEG - Session {session_id}")
        self.logger.error(f"   {error}")
    
    def file_operation(self, operation: str, path: str, size: Optional[int] = None, **extra):
        """Log opération fichier"""
        size_str = f" ({size / 1024 / 1024:.2f} MB)" if size else ""
        self.logger.info(f"{operation}: {path}{size_str}", extra={'extra_data': extra} if extra else {})
    
    def git_operation(self, operation: str, **extra):
        """Log opération Git"""
        self.logger.info(f"GIT: {operation}", extra={'extra_data': extra} if extra else {})
    
    def background_task(self, task_name: str, action: str, **extra):
        """Log tâche en arrière-plan"""
        self.logger.info(f"BACKGROUND [{task_name}]: {action}", extra={'extra_data': extra} if extra else {})
    
    def model_operation(self, operation: str, username: str, **extra):
        """Log opération sur un modèle"""
        self.logger.info(f"MODEL [{operation}]: {username}", extra={'extra_data': extra} if extra else {})


# Instance globale
logger = AppLogger()

# Export du logger
__all__ = ['logger', 'Colors']
