"""Protection locale contre les tentatives répétées de connexion."""
from __future__ import annotations

import math
import threading
import time
from collections import deque


class LoginAttemptLimiter:
    """Limiteur glissant en mémoire, adapté au déploiement mono-processus d'Oubliez-moi."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 15 * 60,
                 clock=time.monotonic, max_clients: int = 10_000):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.clock = clock
        self.max_clients = max_clients
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def consume(self, client: str) -> int:
        """Réserve une tentative et renvoie le délai de blocage restant (0 si autorisée)."""
        now = self.clock()
        cutoff = now - self.window_seconds
        with self._lock:
            # Nettoyage borné : les clés inactives ne restent pas indéfiniment en mémoire.
            for key in list(self._attempts):
                history = self._attempts[key]
                while history and history[0] <= cutoff:
                    history.popleft()
                if not history:
                    del self._attempts[key]

            history = self._attempts.get(client)
            if history and len(history) >= self.max_attempts:
                return max(1, math.ceil(self.window_seconds - (now - history[0])))

            if history is None:
                history = self._attempts[client] = deque()
            history.append(now)
            self._attempts.move_to_end(client) if hasattr(self._attempts, "move_to_end") else None
            while len(self._attempts) > self.max_clients:
                self._attempts.pop(next(iter(self._attempts)))
            return 0

    def reset(self, client: str) -> None:
        with self._lock:
            self._attempts.pop(client, None)


login_attempt_limiter = LoginAttemptLimiter()
