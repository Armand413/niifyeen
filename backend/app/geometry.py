"""Géométrie des lignes et zones (coordonnées normalisées 0-1 dans l'image de la caméra, origine en haut à gauche)."""
from typing import List, Optional, Sequence, Tuple

Point = Tuple[float, float]


def cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def side(a: Point, b: Point, p: Point) -> int:
    """Côté de p par rapport à la droite a->b. Le côté « 1 » est dans la direction (-dy, dx) de la normale
    (pour une ligne tracée de haut en bas à l'écran, c'est le côté gauche de l'écran) ; -1 est l'autre côté ; 0 = sur la droite."""
    c = cross(a, b, p)
    return 1 if c > 1e-12 else (-1 if c < -1e-12 else 0)


def crossing(a: Point, b: Point, prev: Point, cur: Point) -> int:
    """Le déplacement prev->cur coupe-t-il le segment [a, b] ?

    Retourne 1 si le passage va vers le côté « 1 » (voir ``side``), -1 vers le côté « -1 », 0 s'il n'y a pas de passage.
    Un point exactement sur la droite ne compte jamais comme un passage : l'appelant doit alors garder comme
    référence la dernière position située d'un côté ou de l'autre (voir ``AnalyticsEngine``).
    """
    s_prev, s_cur = side(a, b, prev), side(a, b, cur)
    if s_cur == 0 or s_prev == s_cur:
        return 0
    if s_prev == 0:
        return 0
    # Les deux points sont de part et d'autre de la droite : le segment de déplacement coupe-t-il [a, b] ?
    if side(prev, cur, a) == side(prev, cur, b) and side(prev, cur, a) != 0:
        return 0
    return 1 if s_cur > s_prev else -1


def point_in_polygon(p: Point, poly: Sequence[Point]) -> bool:
    """Test du rayon (bord inclus à l'épsilon près)."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def polygon_area(poly: Sequence[Point]) -> float:
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    d1, d2 = cross(p3, p4, p1), cross(p3, p4, p2)
    d3, d4 = cross(p1, p2, p3), cross(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0


def is_simple_polygon(poly: Sequence[Point]) -> bool:
    """Vrai si aucun côté ne coupe un autre côté non adjacent (pas de « nœud papillon »)."""
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            if _segments_intersect(a, b, poly[j], poly[(j + 1) % n]):
                return False
    return True
