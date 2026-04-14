"""
Fonctions utilitaires pour P-StreamRec
"""


def format_bytes(bytes_value: int) -> str:
    """
    Formate une taille en bytes de manière lisible (MB ou GB si > 1000 MB)

    Args:
        bytes_value: Taille en bytes

    Returns:
        Chaîne formatée (ex: "1.5 GB", "256 MB")
    """
    mb = bytes_value / (1024 * 1024)

    if mb >= 1000:
        gb = mb / 1024
        return f"{gb:.2f} GB"
    else:
        return f"{mb:.1f} MB"
