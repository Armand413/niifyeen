# NiifYéén 2.0 — Audit, architecture et plan

Document de pilotage de l'évolution vers « NiifYéén 2.0 ». Il est mis à jour à chaque module livré.
Audit réalisé en lisant le code (et en le testant), non en se fiant à la documentation.

**Légende** — *Existant* : fonctionne et est testé (avec des simulations). *À améliorer* : présent mais incomplet.
*À développer* : absent. *Fait (2.0)* : livré dans cette phase.

> Tout ce qui est marqué « testé » l'est avec des caméras et des modèles **simulés** et, pour les pages, sous un
> faux navigateur (jsdom). Rien n'a encore été validé avec de vraies caméras, de vrais modèles, ni visuellement
> dans un vrai navigateur.

## 1. Audit fonctionnel

| Module | Fonctionnalité | État initial | État actuel |
| --- | --- | --- | --- |
| **1. Supervision** | Flux simultanés | Existant (MJPEG, 1 connexion HTTP par caméra : limite ≈ 6 par navigateur) | **Fait (2.0)** : un seul WebSocket multiplexé |
| | Dispositions 1/2/4/6/9/16, pagination | À développer | **Fait (2.0)** |
| | Plein écran d'une caméra, mode moniteur | À développer | **Fait (2.0)** |
| | Nom, localisation, état | Nom seul | **Fait (2.0)** (+ `location`, état, FPS, latence) |
| | Personnes par flux, cibles détectées | Partiel (liste brute) | **Fait (2.0)** |
| | Fil d'événements | À améliorer (table de détections brutes) | **Fait (2.0)** : événements persistants |
| | Détails d'une caméra | À développer | **Fait (2.0)** |
| | Annotations on/off | À développer | **Fait (2.0)** (image brute encodée à la demande) |
| | Thème clair/sombre, responsive | À développer | **Fait (2.0)** |
| **2. Caméras** | Configuration persistante, ajout / modification / suppression / désactivation | Fichier `cameras.json`, redémarrage obligatoire | **Fait (2.0)** : table `cameras`, interface `/admin/cameras`, application à chaud |
| | Webcam, MJPEG, caméras d'autres PC | Existant (`tools/camera_server.py`) | **Fait (2.0)** : intégré à l'interface (type « autre ordinateur », jeton) |
| | RTSP | Non testé | **Partiel** : accepté et validé ; test de connexion disponible ; **non essayé avec du matériel RTSP réel** |
| | Test de connexion, aperçu avant enregistrement | À développer | **Fait (2.0)** (résolution, images/s, délai, aperçu, messages d'aide) |
| | Déconnexion détectée, reconnexion | Existant | **Amélioré (2.0)** : état, événements « hors ligne / rétablie », relance manuelle |
| | FPS réels, latence | À développer | **Fait (2.0)** (FPS et latence de *traitement*) |
| | Résolution demandée, cadence de traitement par caméra | Taille de flux globale seulement | **Fait (2.0)** : résolution de capture et cadence maximale par caméra ; taille du flux diffusé reste globale |
| | Affectation à une zone | À développer | **Fait (2.0)** : champ « zone » (texte libre) ; zones structurées avec le Module 5 |
| | ONVIF | À développer | **Partiel (2.0)** : découverte réseau (WS-Discovery) ; récupération du flux non faite ; non essayé avec du matériel réel |
| **3. Personnes** | Enrôlement (nom, infos, photos, consentement, date) | Existant | Existant |
| | Prénom, matricule, statut actif/inactif | À développer | **Fait (2.0)** (matricule unique, détection de doublon par visage) |
| | Modifier une fiche | À développer (création et suppression seulement) | **Fait (2.0)** (+ ajout/retrait de photos de référence) |
| | Fiche individuelle (page, historique, zones) | Partiel (fiche dans la carte de la cible) | **Fait (2.0)** : `/people/{id}` |
| | Statuts « probable / à vérifier / inconnu » | À développer (seuil binaire) | **Fait (2.0)** (seuils à calibrer sur de vraies photos) |
| | Liaison des événements par **identifiant** | À développer (liaison par **nom**, risque d'homonymes) | **Fait (2.0)** : `detections.participant_id`, migration sans perte |
| **4. Cibles** | Multi-photos, 8 cibles, couleurs, retrait individuel | Existant | Existant |
| | Choisir une personne déjà enrôlée comme cible | À développer | **Fait (2.0)** (sans photo, via les empreintes de la fiche) |
| | Statuts, dernière caméra / heure, détections associées | Partiel | **Fait (2.0)** : liens fiche + historique de la personne |
| | Sauvegarde / reprise de session, recherche dans l'historique | À développer | **Fait (2.0)** : session = identifiants de personnes enrôlées seulement ; filtre historique limité au tableau des passages |
| | Capacité configurable | Constante `MAX_TARGETS` | **Fait (2.0)** : 1 à 32, à chaud, persistée |
| **5. Plan** | Plan SVG, positions, trajets colorés multi-cibles | Existant | Existant |
| | Import de plan, étages, placement à la souris, zones, champ de vision | À développer | **Fait (2.0)** : champ de vision réglé à la main (zones : Module 6, sur l'image caméra) |
| | Rejouer un trajet, analyses de déplacements | À développer | **Fait (2.0)** (passages, temps cumulé, trajets fréquents) |
| **6. Événements vidéo** | Lignes, zones, occupation, affluence, caméra masquée | À développer | **Fait (2.0)**, testé en simulation : lignes de comptage, zones (capacité, interdites), affluence par 5 min, caméra masquée/figée ; seuils non calibrés |
| **7. Alertes** | Alertes visuelles/sonores | Existant (page caméras, éphémères) | **Fait (2.0)**, testé en simulation : table `alerts`, règles par type, regroupement, états, historique, notes, filtres/recherche, pastille et son partout ; e-mail/Telegram optionnels (testés avec de faux serveurs) ; *présence prolongée* non générée |
| **8. Historique** | Passages, filtres, CSV, trajets | Existant | Existant |
| | PDF, dossier d'événement, chronologie visuelle, recherche par session/personne | À développer | **Fait (2.0)**, testé en simulation : page `/investigation`, filtres combinables, détail avec plan et alertes, CSV/PDF, rapport de session, dossiers (références) ; extrait vidéo via le Module 9 |
| **9. Enregistrement vidéo** | Enregistrement, relecture, export | À développer | **Fait (2.0)**, testé en simulation : désactivé par défaut, manuel / sur événement / plage horaire, rétention, liste par caméra, lecteur avec chronologie, lien depuis l'investigation, export AVI ; images brutes 640x360, rien de validé avec de vraies caméras |
| **10. Statistiques** | Synthèse (totaux, par caméra, courbe) | Partiel (`/history`, `/logs`) | **Fait (2.0)**, testé en simulation : page `/statistics` ; observations / passages / personnes enrôlées uniques distingués ; profil horaire, semaine type, carte de chaleur, comparaison, graphe de transitions, lignes et zones ; données anonymes, CSV ; inconnus non dédoublonnés |
| **11. Démonstration** | Mode démo, scénario | À développer (`camera_server --test-pattern` seulement) | **Fait (2.0)**, testé en simulation : `tools/run_demo.py`, base et enregistrements séparés, caméras virtuelles, scénario en boucle de 2 min, bandeau permanent, relance / réinitialisation ; reconnaissance et détection simulées |
| **12. Rapports** | PDF, rapports paramétrables | À développer (CSV des passages seulement) | **Fait (2.0)**, testé en simulation : page `/reports`, 8 sections au choix, aperçu, PDF anonyme avec histogramme, modèles enregistrés ; pas de rapport planifié ni envoyé par e-mail |
| **13. Interface** | Navigation, cohérence | Pages HTML hétérogènes | **Fait (2.0)** : toutes les pages passent par la coque commune (menu, thème clair / sombre) ; Accueil, Plan et cheminement, Journal et Historique portés avec `legacy.css`, comportement inchangé ; regardé dans Chromium (1366 px et 390 px, deux thèmes) ; pas de test sur Firefox/Safari |
| **14. Avancées** | Couverture, graphe de transitions, multi-sites… | Fait (2.0) : présence prolongée et couverture (transitions déjà faites au Module 10) ; zones sur le plan et multi-sites NON faits | Voir README « Fonctions avancées » |

## 2. Ajustements d'architecture

| # | Ajustement | Pourquoi | Statut |
| --- | --- | --- | --- |
| A1 | **Flux vidéo multiplexé** `/ws/video` (JPEG binaires, abonnement par client) | Un navigateur n'ouvre qu'environ 6 connexions HTTP/1.1 par serveur : une grille de 16 flux MJPEG se bloquerait. Réduit aussi la bande passante (seules les caméras affichées sont envoyées). | **Fait** |
| A2 | **Statistiques et état par caméra** (`status`, FPS, latence, personnes, reconnexions) | Base de la supervision, des alertes de panne, des rapports. | **Fait** |
| A3 | **Événements persistants** (`events`, `EventFeed`, `SupervisionMonitor`) | Fondation du centre d'alertes, de l'investigation et des rapports ; sépare les faits lisibles du flux brut d'observations. | **Fait** (alertes à bâtir dessus) |
| A4 | **Système de design + coque** (`app.css`, `shell.js`) | Cohérence visuelle, thème, navigation, composants réutilisés par toutes les pages. | **Amorcé** |
| A5 | **Gestionnaire de caméras en base** (`CameraManager`, ajout/retrait/redémarrage à chaud, champ `site_id`) | Module 2 : configurations persistantes, administration sans redémarrage ; `site_id` prévu pour le multi-sites. | **Fait** |
| A6 | **`participant_id` dans `detections` et les événements** | Module 3 : relier les événements à l'identifiant, non au nom (homonymes). | **Fait** |
| A7 | **Couche d'analyse sur les pistes** (étape pluggable après le suivi : lignes, zones, comptage) | Module 6 : calculer sur les positions de pistes déjà produites, sans relancer de modèle. | **Fait** (`analytics.py`) |
| A8 | **Plans et zones en base** (étages, zones, placement des caméras, champ de vision) | Modules 5, 6, 14. | **Étages, placement, champ de vision et zones faits** ; zones sur le plan : Module 14 |
| A10 | **Détecteur partagé** (un seul modèle YOLO chargé, un suivi par caméra) | Aujourd'hui chaque caméra charge sa propre copie du modèle : coûteux en mémoire et au démarrage à chaud. | À faire (avant d'ajouter beaucoup de caméras) |
| A9 | **Enregistreur vidéo** (thread distinct, segments `.nyr`, stockage + rétention) | Module 9 ; ne bloque pas l'analyse. | **Fait** |

## 3. Plan et dépendances

```
A2+A3+A4 (fait) ─► M1 Supervision (fait) ─► M2 Caméras (A5, fait) ─► M3 Personnes (A6) ─► M4 Cibles ─► M5 Carte
                                           └► M6 Événements vidéo (A7, A8) ─► M7 Alertes ─► M8 Investigation
                                                                                         └► M10 Stats ─► M12 Rapports
M9 Enregistrement (A9) ─► liens depuis M8        M11 Démo : après M1–M5 (scénario complet)      M14 : en dernier
```

Ordre retenu (conforme aux priorités demandées) : **M1 ✔ → M2 ✔ → M3 → M4 → M5 → M6 → M7 → M8 → M10 → M12 → M9 → M11 → M14**,
la démonstration (M11) étant avancée si une présentation approche, puisqu'elle ne dépend que des modules déjà livrés.

## 4. Initiatives proposées (à valider)

- **Alerte de « personne non autorisée » à partir du statut `inactif`** (Module 3) : une personne désactivée
  mais reconnue déclenche un événement. Utile, faible coût une fois le statut ajouté.
- **Calibration des seuils assistée** : script qui mesure faux positifs / faux négatifs sur un dossier de photos
  étiquetées. Indispensable avant toute présentation « fiable » de la reconnaissance.
- **Journal d'audit** (qui a consulté une fiche, exporté un rapport) : à prévoir avant d'envisager une authentification.
