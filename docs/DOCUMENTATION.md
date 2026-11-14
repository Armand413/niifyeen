# NiifYéén — documentation technique complète

Prototype Python de supervision vidéo multi-caméras, avec une interface web en français, détection de personnes, suivi local, événements en direct et historique SQLite.

Le dépôt contient également des fonctions de reconnaissance faciale et de recherche par photo. Il ne s’agit donc pas d’un système exclusivement anonyme. Ce document décrit l’état actuel du code ; les instructions de démarrage concernent le serveur local et la consultation des flux, sans guide de configuration de l’identification biométrique.

## Sommaire

- [Prérequis](#prérequis)
- [Lancement rapide](#lancement-rapide)
- [Installation sur une autre machine](#installation-sur-une-autre-machine)
- [Interface](#interface)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Données](#données)
- [Gestion des caméras](#gestion-des-caméras)
- [Centre de supervision](#centre-de-supervision)
- [Caméras sur d’autres ordinateurs](#caméras-sur-dautres-ordinateurs)
- [Cibles multiples](#cibles-multiples)
- [Fiche de la cible](#fiche-de-la-cible)
- [Historique des détections](#historique-des-détections)
- [Données personnelles et historique Git](#données-personnelles-et-historique-git)
- [API](#api)
- [Tests](#tests)
- [Dépannage](#dépannage)
- [Limites](#limites)

## Prérequis

- Windows est l’environnement de développement actuel. Certains chemins et outils de capture utilisent des composants propres à Windows.
- Python **3.10 minimum** pour la syntaxe employée. L’environnement local inspecté utilise Python **3.10.20**.
- Un navigateur récent.
- Une webcam ou des sources vidéo réseau accessibles pour tester les flux réels.
- Les bibliothèques Python et les modèles attendus par le code.

Le dépôt ne fournit pas de minimum matériel chiffré. La charge dépend notamment du nombre de caméras, des modèles chargés et du processeur. La configuration locale de PyTorch est une version CPU ; aucun GPU n’est requis par le code de lancement.

## Lancement rapide

Les commandes suivantes s’exécutent dans **PowerShell**, depuis la racine du projet.

### 1. Ouvrir le dossier

```powershell
Set-Location D:\HackEPO\niifyeen
```

Sur une autre machine, remplacer ce chemin par celui du projet.

### 2. Vérifier l’environnement existant

```powershell
.\venv\Scripts\python.exe --version
.\venv\Scripts\python.exe -m pip check
```

L’appel direct à l’interpréteur de `venv` évite d’avoir à activer l’environnement et ne demande pas de modifier la politique d’exécution PowerShell.

### 3. Configurer les caméras (facultatif)

Les caméras se gèrent depuis la page **Gestion des caméras** (`/admin/cameras`, voir ci-dessous). Au **tout premier démarrage uniquement**, le fichier `cameras.json` (ignoré par Git) est importé dans la base ; ensuite il n'est plus relu. Pour préparer cet import :

```powershell
Copy-Item cameras.example.json cameras.json
```

Sans ce fichier, une seule caméra est créée : la webcam d’index `0` (identifiant `entre`). Modifier `cameras.json` après le premier démarrage n'a **aucun effet** : utiliser l'interface.

### 4. Démarrer le serveur

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

**Le démarrage ouvre les caméras configurées.** Le processus crée ou met à niveau les tables SQLite, démarre le suivi des cibles et l’enregistrement des observations, puis supprime périodiquement les observations plus anciennes que la durée de conservation (30 jours par défaut).

Conserver le terminal ouvert, puis consulter :

| Adresse locale | Contenu |
| --- | --- |
| <http://127.0.0.1:8000/> | Accueil actuel |
| <http://127.0.0.1:8000/cameras> | Flux vidéo, carte et événements |
| <http://127.0.0.1:8000/docs> | Documentation interactive des routes HTTP |
| <http://127.0.0.1:8000/redoc> | Autre présentation de la documentation HTTP |

Le WebSocket n’est pas décrit dans la documentation OpenAPI générée.

### 5. Arrêter ou redémarrer

- Arrêter avec **Ctrl + C** dans le terminal du serveur.
- Après une modification Python, relancer la commande de démarrage.
- Après une modification CSS ou HTML, recharger le navigateur avec **Ctrl + F5**.

Garder un seul processus serveur pour les essais : plusieurs workers ouvriraient chacun les caméras et disposeraient d’états mémoire indépendants. Le rechargement automatique n’est pas activé dans la commande proposée afin d’éviter des réouvertures répétées des périphériques.

## Installation sur une autre machine

Le dossier `venv` est local à une machine et ne doit pas être copié comme méthode d’installation. Le dépôt fournit `requirements.txt` (dépendances d’exécution, bornes minimales) et `requirements-dev.txt` (de quoi lancer les tests, sans les modèles lourds).

Pour créer un environnement neuf si `venv` n’existe pas :

```powershell
py -3.10 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Les bibliothèques nécessaires à l’application comprennent :

| Bibliothèque | Rôle |
| --- | --- |
| FastAPI, Uvicorn | Application HTTP et serveur ASGI |
| websockets | Prise en charge WebSocket du serveur |
| python-multipart | Lecture des formulaires contenant des fichiers |
| NumPy, OpenCV | Images, tableaux numériques et capture vidéo |
| Ultralytics, PyTorch, torchvision | Détection et suivi de personnes |
| SQLAlchemy | Accès à SQLite |
| InsightFace, ONNX Runtime | Modules de vision présents dans le dépôt |

`requirements.txt` n’impose que des versions minimales et n’a pas été validé sur une machine vierge. Pour figer les versions d’un environnement qui fonctionne, l’exporter **depuis cet environnement** :

```powershell
.\venv\Scripts\python.exe -m pip freeze > requirements-local.txt
```

Puis, après vérification de ce fichier pour la machine destinataire :

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-local.txt
.\venv\Scripts\python.exe -m pip check
```

Un export peut contenir des chemins locaux ou des variantes CPU/GPU non portables. Une installation sur un environnement vierge n’a pas été validée ; les dépendances natives peuvent nécessiter des composants de compilation ou des paquets adaptés à la plateforme.

### Fichiers nécessaires

- `yolov8n.pt` : poids du détecteur. Ignoré par Git ; s’il est absent de la racine, Ultralytics le télécharge au premier lancement.
- `models_insightface/` : modèles de reconnaissance faciale, ignorés par Git ; InsightFace les télécharge au premier usage s’ils manquent.
- `niifyeen.db` : base locale, **ignorée par Git** (elle contient des données personnelles). Une base absente est créée au démarrage ; une ancienne base est mise à niveau automatiquement.

Des bibliothèques peuvent tenter des téléchargements à la première utilisation si des ressources manquent. Un démarrage entièrement hors ligne suppose que les dépendances et modèles soient déjà disponibles. Les conditions d’utilisation des bibliothèques et des poids restent à vérifier séparément ; aucune licence de projet n’est fournie dans le dépôt.

## Interface

### Accueil

`backend/app/static/index.html` contient le formulaire d’enrôlement (avec case de **consentement obligatoire**), la recherche par photo, la liste des personnes enrôlées avec suppression, ainsi qu’un lien vers les caméras. Les réponses des formulaires sont affichées sous forme de JSON.

### Page caméras

`backend/app/static/cameras.html` présente :

- Une mosaïque de flux vidéo MJPEG.
- L’état de connexion WebSocket du navigateur au serveur.
- Un **plan du site** qui montre sur quelle caméra chaque cible est identifiée et trace son trajet (une couleur par cible) entre les caméras (voir ci-dessous).
- Un tableau d’événements en direct et un historique de parcours en mémoire.
- Des notifications visuelles et un signal sonore, que le navigateur peut bloquer tant que l’utilisateur n’a pas interagi avec la page.

L’état « WS connecté » confirme la connexion au serveur, **pas** le bon fonctionnement de chaque caméra. Le tableau de parcours affiché n’est pas la liste complète des détections stockées en base.

### Historique

`backend/app/static/history.html` (page `/history`) : passages devant les caméras, synthèse, export CSV et trajets des cibles. Voir [Historique des détections](#historique-des-détections). Le journal agrégé `/logs` reste disponible et anonyme.

`app.css` (variables du thème clair / sombre, composants) et `shell.js` (menu, thème, alertes) sont communs à toutes les pages ; `legacy.css` habille les anciennes pages (Accueil, Plan et cheminement, Journal, Historique) avec ces mêmes variables. Il n’y a ni framework frontend ni étape de compilation JavaScript.

## Configuration

Tout est centralisé dans `backend/app/config.py`. Les valeurs peuvent être surchargées par des variables d’environnement ; aucun fichier `.env` n’est lu automatiquement.

| Élément | Où / comment |
| --- | --- |
| Caméras (sources, noms, zones, position, cadence) | Page `/admin/cameras`, stockées en base. `cameras.json` (modèle : `cameras.example.json`) n'est lu qu'à l'import du premier démarrage. Champs : `id`, `source`, `label`, `location`, `x`, `y` |
| Nombre maximal de caméras | `NIIFYEEN_MAX_CAMERAS` (32) |
| Base de données | `NIIFYEEN_DB` (par défaut `niifyeen.db` à la racine du projet) |
| Dossier des modèles InsightFace | `NIIFYEEN_MODELS_DIR` |
| Maintien d'identité sans visage | `NIIFYEEN_IDENTITY_HOLD_SEC` (10 s, 1–300) : une cible/personne reconnue reste identifiée tant que la même silhouette est suivie ; marquée `~suivi` à l'écran (`tracked_only` dans l'événement). **Un visage net qui ne correspond plus** retire l'identité après `NIIFYEEN_IDENTITY_CONTRADICTIONS` confirmations (2). Risque restant : un échange de piste sans visage visible (croisement, occlusion) transmet l'identité à la mauvaise personne jusqu'à la fin du délai. |
| Qualité minimale du visage | `NIIFYEEN_FACE_MIN_SIZE_PX` (32 px) et `NIIFYEEN_FACE_MIN_DET_SCORE` (0,5) : un visage plus petit ou moins net est ignoré (ni reconnu, ni utilisé pour contredire), car son empreinte est trop bruitée. |
| Seuils de similarité | `NIIFYEEN_DB_THRESHOLD` (0,35), `NIIFYEEN_TARGET_THRESHOLD` (0,40), `NIIFYEEN_REID_THRESHOLD` (0,45) |
| Conservation des observations | `NIIFYEEN_RETENTION_DAYS` (30 ; `0` = illimitée) |
| Taille maximale d’une photo envoyée | `NIIFYEEN_MAX_UPLOAD_BYTES` (5 Mo) |
| Similarité minimale pour afficher une fiche | `NIIFYEEN_PROFILE_THRESHOLD` (égale au seuil de la cible, 0,40) |
| Nombre de cibles / doublon | `MAX_TARGETS` = 8 dans `config.py` ; `NIIFYEEN_TARGET_DUPLICATE_THRESHOLD` (0,60) : une photo aussi proche d’une cible existante est refusée |
| Taille des images diffusées | `NIIFYEEN_STREAM_WIDTH` (640) et `NIIFYEEN_STREAM_HEIGHT` (360) |
| Pause qui sépare deux passages | `NIIFYEEN_HISTORY_GAP_SEC` (10 s) |
| Afficher les noms dans l’historique | `NIIFYEEN_HISTORY_IDENTITIES` (`1` ; `0` pour masquer) |
| Traitement et dimensions des images | `backend/app/person_pipeline.py` et son instanciation dans `main.py` |
| Diffusion MJPEG | `backend/app/video_stream.py` |
| Présentation web | `backend/app/static/` |

Les chemins sont **absolus** (calculés depuis la racine du projet) : le serveur peut être lancé depuis n’importe quel dossier. Un fichier `cameras.json` invalide empêche le démarrage avec un message explicite, tant que l'import n'a pas eu lieu.

Les seuils de similarité n’ont pas été validés sur un jeu de test. Dans un système qui désigne une personne, un seuil trop bas produit des faux positifs : à calibrer avec des données réelles avant tout usage.

Les identifiants de caméra sont les mêmes pour les flux, le journal, les événements et les métadonnées d’affichage (`camera_meta`) ; ils ne changent jamais après la création.

## Architecture

```text
niifyeen/
├── README.md
├── .gitignore
├── requirements.txt / requirements-dev.txt
├── cameras.example.json        # modèle ; cameras.json (local) est ignoré par Git
├── .github/workflows/tests.yml
├── backend/app/
│   ├── main.py                 # Routes HTTP/WebSocket, cycle de vie (lifespan)
│   ├── config.py               # Chemins, caméras, seuils, limites
│   ├── person_pipeline.py      # Capture et analyse par caméra (un thread chacune)
│   ├── targets.py              # Registre des cibles (plusieurs en même temps)
│   ├── target_path.py          # Parcours de chaque cible (thread dédié, testable)
│   ├── db_logger.py            # Journalisation périodique + rétention
│   ├── history.py              # Passages, synthèse, trajets, export CSV
│   ├── camera_manager.py       # Caméras en base : validation, ajout/modification/retrait à chaud
│   ├── camera_probe.py         # Test de connexion, détection des webcams, découverte ONVIF
│   ├── supervision.py          # État des caméras, personnes visibles, moniteur d'événements
│   ├── events.py               # Fil d'événements persistant (table events)
│   ├── investigation.py        # Chronologie filtrable, détail d'événement, dossiers, rapports
│   ├── alerts.py               # Centre d'alertes : règles, regroupement, états, notifications e-mail/Telegram
│   ├── geometry.py             # Passage de ligne, point dans un polygone
│   ├── analytics.py            # Lignes et zones virtuelles : comptage, occupation, affluence, caméra masquée
│   ├── target_profile.py       # Fiche de la cible : correspondance avec la base
│   ├── video_stream.py         # Envoi asynchrone des images MJPEG
│   ├── vision.py               # InsightFace (chargé à la demande), similarité
│   ├── reid.py                 # Rapprochement inter-caméras
│   ├── db.py                   # Modèles SQLAlchemy, migration, purge, suppression
│   ├── activity_log.py         # Agrégats par minute
│   ├── list_cams.py            # Utilitaire Windows de sondage des webcams
│   └── static/                 # supervision, cameras-admin, zones, alerts, investigation, recordings, statistics, reports, index, cameras, logs, history (.html/.js), app.css, legacy.css, shell.js, plan.js, profile.js, targets-view.js, blank-plan.svg
├── docs/NIIFYEEN_2_0_PLAN.md   # Audit, architecture et plan de NiifYéén 2.0 (état d'avancement)
├── tools/camera_server.py      # À copier sur les autres ordinateurs : diffuse leur webcam
└── tests/                      # 516 tests, sans caméra ni modèle
```

### Circulation des données

1. FastAPI démarre un `PersonPipeline` par caméra, dans un thread distinct.
2. OpenCV lit les images ; les modèles existants produisent les événements et les annotations.
3. Le circuit conserve la dernière image JPEG et les derniers événements en mémoire.
4. Les routes vidéo transmettent les images au navigateur ; le WebSocket transmet les événements environ toutes les 100 ms.
5. Un thread dédié (`TargetWorker`) met à jour le parcours de chaque cible environ 10 fois par seconde, **que le navigateur soit ouvert ou non** ; le WebSocket ne fait que lire cet état.
6. Un autre thread écrit les observations dans SQLite, avec un espacement nominal de 0,8 seconde par caméra, piste et mode, et supprime les observations plus anciennes que la durée de conservation.

Si une caméra cesse de fournir des images, ses événements sont vidés (plus d’événements périmés à l’écran) et le flux est rouvert automatiquement après quelques secondes.

La diffusion vidéo n’envoie plus la même instance d’image en boucle. Elle vérifie les nouvelles images à une cadence maximale nominale de 20 images/s par connexion, avec attente asynchrone. Cela limite le trafic inutile mais ne garantit pas 20 images/s de traitement ni une latence donnée.

## Données

La base est un fichier SQLite (`niifyeen.db` à la racine par défaut, voir [Configuration](#configuration)).

| Table | Contenu |
| --- | --- |
| `participants` | Nom, informations et date de consentement enregistrée |
| `embeddings_reference` | Représentations numériques de référence et nom du fichier source |
| `detections` | Caméra, date, piste, mode, libellé, scores et coordonnées |
| `target_moves` | Trajets des cibles : une ligne par première identification ou changement de caméra, rattachée à la « session » d’une cible, avec son libellé (« Cible 2 ») |

Les modes existants sont `TARGET`, `DB` et `UNK`. Un identifiant de piste appartient à une caméra ; les identifiants globaux du module existant sont temporaires et en mémoire. Aucun de ces champs ne garantit une identité exacte.

Points importants :

- Les observations sont persistées, pas les vidéos complètes.
- Les coordonnées `x` et `y` sont normalisées dans l’image, pas des coordonnées géographiques.
- Les dates sont stockées en UTC et sérialisées avec le suffixe `Z`.
- Le parcours courant et les états de suivi en mémoire sont perdus au redémarrage.
- Les photos d’enrôlement ne sont **pas** conservées : seule une empreinte numérique (embedding) et le nom du fichier source sont enregistrés. Ces empreintes restent des données biométriques.
- L’enrôlement exige une case de consentement ; la date de consentement est enregistrée. Il n’y a pas de justificatif de consentement stocké.
- Supprimer une personne (interface ou `DELETE /participants/{id}`) efface ses empreintes et remplace son nom par « inconnu » dans les observations déjà enregistrées.
- Les observations **et les trajets des cibles** sont supprimés automatiquement après `NIIFYEEN_RETENTION_DAYS` jours (30 par défaut).

### Sauvegarde

Arrêter le serveur, puis sauvegarder `niifyeen.db` dans un emplacement protégé. Si des fichiers SQLite annexes existent, les préserver également avec la sauvegarde. Ne pas supprimer la base pour résoudre une erreur de schéma sans sauvegarde préalable.

Au démarrage, `init_db()` crée les tables manquantes, ajoute les colonnes manquantes de `detections` (migration simple pour SQLite) et crée un index sur la date. Cette migration ne couvre que les colonnes de `detections` ; sauvegarder la base avant tout changement de schéma plus important.

## Gestion des caméras

La page `/admin/cameras` (menu « Gestion des caméras ») remplace l'édition de `cameras.json`. Les caméras sont stockées **en base** : elles persistent entre les redémarrages, et toute modification est appliquée **à chaud**.

**Ce qu'on peut faire.** Ajouter, modifier, désactiver, réactiver, relancer et supprimer une caméra. Types pris en charge : webcam USB du serveur, caméra d'un autre ordinateur (script `camera_server.py`, avec jeton), flux MJPEG/HTTP, caméra IP RTSP. Chaque caméra a un nom, une zone du bâtiment, une position sur le plan et, en réglages avancés, la résolution demandée à la webcam et une **cadence de traitement maximale** (économise le processeur ; pendant l'attente, les images en trop sont lues et jetées pour ne pas accumuler de retard). Le tableau affiche en direct l'état, les images par seconde, la latence de traitement et la résolution.

**Appliquer sans tout interrompre.** Seule la caméra concernée est arrêtée puis relancée, et seulement si sa source, sa résolution, sa cadence ou son activation change ; renommer ou déplacer une caméra ne coupe pas son flux. Les pages de supervision et de plan suivent automatiquement les ajouts, retraits et désactivations. Chaque ajout, suppression, activation ou désactivation laisse un événement dans le fil de supervision.

**Tester avant d'enregistrer.** « Tester la connexion » ouvre la source, lit des images et affiche la résolution, les images par seconde, le délai de première image et un **aperçu** ; en cas d'échec, un message explique les causes probables (adresse, port, pare-feu, jeton, identifiants). Si la source est déjà utilisée par une caméra en service, l'image de cette caméra est réutilisée au lieu de la rouvrir (une webcam ne s'ouvre qu'une fois). Le test d'une caméra existante utilise sa source enregistrée **sans la renvoyer au navigateur**.

**Détection.** « Détecter les webcams » liste les webcams du serveur (celles déjà en service ne sont pas rouvertes). « Rechercher des caméras ONVIF » envoie une découverte réseau (WS-Discovery, multicast UDP) et liste les caméras qui répondent, avec leur adresse, leur nom et leur modèle, en signalant celles déjà configurées. **Limites** : la découverte ne récupère pas l'adresse du flux (chemin RTSP, identifiants : ils dépendent de la marque), elle est à saisir à la main ; elle suppose que le pare-feu et le réseau laissent passer le multicast, et n'a pas pu être essayée avec du matériel réel.

**Sécurité des sources.**

- Sources acceptées : un index de webcam (0 à 31) ou une adresse `http://`, `https://`, `rtsp://`, `rtsps://`. Les **fichiers locaux sont refusés** (OpenCV les lirait volontiers) ainsi que tout autre schéma.
- **Jetons et mots de passe ne sont jamais renvoyés** par l'API ni affichés : le tableau montre `http://192.168.1.31:8081/video?…`. Pour changer de source, saisir la nouvelle ; la laisser vide la conserve. Ils sont toutefois **enregistrés en clair dans la base** (`niifyeen.db`, ignorée par Git, comme l'était `cameras.json`) : protéger ce fichier.
- Le test de connexion et la découverte font sonder le réseau **depuis le serveur**. Sans authentification, toute personne pouvant atteindre le serveur peut les utiliser : ne pas l'exposer hors de `127.0.0.1`.
- Deux caméras ne peuvent pas partager la même source (conflit explicite), et le nombre de caméras est limité (`NIIFYEEN_MAX_CAMERAS`, 32).

**Import de `cameras.json`.** Au tout premier démarrage uniquement ; un réglage mémorisé en base empêche tout nouvel import (supprimer toutes les caméras ne recrée donc pas la webcam par défaut). Pour réimporter, ajouter les caméras par l'interface.

**Limites actuelles.** Chaque caméra charge sa propre copie du détecteur (YOLO) : ajouter beaucoup de caméras consomme mémoire et processeur, et le démarrage d'une caméra prend quelques secondes ; un détecteur partagé est prévu. La résolution des images diffusées reste globale (`NIIFYEEN_STREAM_WIDTH/HEIGHT`). Le champ `site_id` est enregistré (préparation du multi-sites) mais pas encore exploité. L'apparence n'a pas été vérifiée dans un vrai navigateur ; le flux d'un autre ordinateur a été testé avec le script de diffusion en image de test, pas avec une vraie webcam ni du matériel RTSP/ONVIF.

## Centre de supervision

La page `/supervision` regroupe l'essentiel sur un seul écran. C'est le premier module de NiifYéén 2.0 (voir `docs/NIIFYEEN_2_0_PLAN.md`) et la base d'une interface commune (`app.css`, `shell.js` : barre latérale, thème, icônes, notifications).

**Grille des caméras.** Dispositions de 1, 2, 4, 6, 9 ou 16 caméras par page, avec pagination (flèches ← → du clavier) quand il y a plus de caméras que d'emplacements. Chaque tuile affiche le nom et le lieu de la caméra, son état (en ligne, connexion, reconnexion, hors ligne), les images par seconde, le nombre de personnes détectées, et un cadre de la couleur de la cible lorsqu'une cible y est détectée. Un clic sélectionne la caméra ; un double-clic ou le bouton ⤢ l'affiche en plein écran.

**Panneau latéral.** *Événements* : fil en temps réel (cibles repérées / déplacées / perdues, personnes enrôlées reconnues, caméras hors ligne ou rétablies), filtrable par famille ; cliquer un événement ouvre la caméra concernée. *Caméra* : identité, lieu, état, source (sans jeton ni mot de passe), résolution, images par seconde, **latence de traitement**, personnes visibles, reconnexions, âge de la dernière image, cibles et personnes présentes, derniers événements. *Cibles* : état de chaque cible et dernière caméra.

**Affichage.** Annotations (cadres et noms) affichables ou masquables, thème sombre (par défaut) ou clair, **mode moniteur** (grille seule, plein écran, menus masqués ; la barre d'outils réapparaît au survol) pour les grands écrans, adaptation aux tablettes. Les préférences (disposition, annotations, thème, son) sont mémorisées par le navigateur. Raccourcis : **F** plein écran, **M** mode moniteur, **A** annotations, **←/→** page.

**Événements de supervision.** Table `events` (persistante, purgée avec la rétention). Générés par `SupervisionMonitor` sans répétition : une caméra n'est déclarée *hors ligne* qu'après 5 s sans image (15 s si elle n'a jamais fonctionné) et *rétablie* à son retour ; une cible n'est déclarée *perdue* qu'après 8 s ; une personne enrôlée n'est annoncée qu'une fois par minute et par caméra. Les noms respectent `NIIFYEEN_HISTORY_IDENTITIES`.

**Flux vidéo multiplexé (`/ws/video`).** Un navigateur n'ouvre qu'environ 6 connexions HTTP par serveur : afficher 16 flux MJPEG séparés bloquerait la page. La page ouvre donc **un seul WebSocket** et y déclare les caméras visibles : `{"subscribe": ["entre","salle"], "annotations": true, "fps": 10}`. Le serveur répond par des messages binaires : 1 octet (longueur de l'identifiant), l'identifiant, puis l'image JPEG. Seules les caméras affichées sont envoyées, à une cadence adaptée à la disposition (de 15 i/s pour 1 caméra à 5 i/s pour 16), et plus rien n'est envoyé quand l'onglet est caché. Les images **sans annotations** ne sont encodées que tant qu'au moins un client les demande. La route `/video/{id}` (MJPEG) reste disponible.

**Configuration.** Chaque caméra a une zone (`location`, 80 caractères max), affichée dans les tuiles et modifiable dans la page de gestion. `NIIFYEEN_STREAM_WIDTH` / `NIIFYEEN_STREAM_HEIGHT` règlent la taille des images diffusées : plus grand = plus net en plein écran sur un grand moniteur, mais plus de bande passante et d'encodage.

Limites et précautions :

- **L'apparence n'a pas été vérifiée dans un vrai navigateur** : la logique de la page est testée sous un faux navigateur (jsdom) contre un vrai serveur, pas son rendu graphique. Les retours visuels (proportions, lisibilité) sont les bienvenus.
- La **latence** affichée est la durée de *traitement* d'une image par le serveur ; elle n'inclut ni le réseau ni le décalage d'une caméra distante. Les images par seconde sont celles du traitement, pas celles du capteur.
- La résolution des images diffusées est la même pour toutes les caméras (taille ci-dessus) ; la détection travaille sur cette image réduite.
- Les personnes reconnues sont encore retrouvées **par leur nom** (pas par identifiant) : la correction est prévue au module « personnes ».
- Comme le reste de l'application, la page n'est protégée par aucune authentification.

## Caméras sur d’autres ordinateurs

Le serveur NiifYéén lit chaque caméra par son adresse. Pour utiliser la webcam d’un autre ordinateur, on lance dessus `tools/camera_server.py`, qui la diffuse en flux vidéo (MJPEG sur HTTP) ; le serveur NiifYéén s’y connecte, comme à une caméra réseau. **Tout le traitement (détection, reconnaissance) reste sur le serveur NiifYéén** : les autres ordinateurs ne font que diffuser.

**Sur chaque autre ordinateur** (Windows, par exemple) :

1. Installer [Python](https://www.python.org/downloads/) 3.10 ou plus, puis dans PowerShell : `python -m pip install opencv-python`.
2. Copier le seul fichier `tools/camera_server.py` (clé USB, partage réseau…).
3. Le lancer (un port différent par ordinateur n'est nécessaire que s'ils partagent la même adresse) :

   ```powershell
   python camera_server.py --token UN_MOT_DE_PASSE
   ```

   Le script affiche les adresses à utiliser, par exemple `"source": "http://192.168.1.31:8081/video?token=UN_MOT_DE_PASSE"`. Options : `--camera 1` (autre webcam), `--port 8082`, `--width`, `--height`, `--fps`, `--quality` (moins de qualité = moins de débit), `--test-pattern` (image de test, sans webcam). Arrêt : Ctrl + C.
4. Autoriser le port dans le pare-feu Windows (PowerShell en administrateur), en limitant l'accès à l'adresse du serveur NiifYéén :

   ```powershell
   New-NetFirewallRule -DisplayName "NiifYeen camera" -Direction Inbound -Protocol TCP -LocalPort 8081 -Action Allow -Profile Private -RemoteAddress ADRESSE_DU_SERVEUR_NIIFYEEN
   ```

   (`-Profile Private` suppose que le réseau est classé « privé » dans Windows.)

**Vérifier depuis le serveur NiifYéén** : ouvrir `http://ADRESSE:8081/?token=…` dans un navigateur. L'aperçu de la caméra doit s'afficher. Sinon : même réseau ? pare-feu ? bonne adresse (`ipconfig`) ?

**Configurer** : ouvrir `/admin/cameras` sur le serveur NiifYéén, « Ajouter une caméra », type « Caméra d'un autre ordinateur », saisir l'adresse IP, le port et le jeton, puis **Tester la connexion** (aperçu et mesures) avant d'enregistrer ; aucun redémarrage n'est nécessaire. Pour un premier démarrage, on peut aussi pré-remplir `cameras.json` (importé une seule fois) :

```json
[
  { "id": "entre",  "source": 0, "label": "Entrée", "x": 0.11, "y": 0.72 },
  { "id": "salle",  "source": "http://192.168.1.31:8081/video?token=UN_MOT_DE_PASSE", "label": "Salle",  "x": 0.63, "y": 0.30 },
  { "id": "sortie", "source": "http://192.168.1.32:8081/video?token=UN_MOT_DE_PASSE", "label": "Sortie", "x": 0.89, "y": 0.72 }
]
```

Si un ordinateur s'éteint ou perd le réseau, l'image « PAS DE SIGNAL » s'affiche, puis le flux se rétablit automatiquement quand il revient (testé).

Précautions et limites :

- **Le flux n'est pas chiffré.** Le jeton n'est qu'une protection de base (il circule en clair sur le réseau) : n'utiliser que sur un réseau de confiance, et limiter le pare-feu au serveur NiifYéén. `cameras.json` contient le jeton : il est ignoré par Git, ne pas le publier.
- **Les adresses IP doivent rester les mêmes.** Si elles viennent du DHCP ou du partage de connexion d'un téléphone, elles peuvent changer à chaque connexion : réserver une adresse fixe dans le routeur ou la configurer sur l'ordinateur, sinon mettre `cameras.json` à jour.
- Chaque caméra consomme de la bande passante (de l'ordre de quelques Mb/s en 640 × 480 à 15 images/s, à vérifier sur votre réseau) ; en Wi-Fi saturé, baisser `--fps`, `--quality` ou `--width`.
- Une webcam ne peut être utilisée que par un programme à la fois : fermer les autres applications qui l'utilisent (visioconférence…).
- Le décalage entre les ordinateurs est de l'ordre d'une fraction de seconde ; le suivi entre caméras en tient compte via la confirmation de changement de caméra (0,8 s).
- Autres solutions compatibles, sans ce script : une application de caméra IP sur téléphone ou OBS/VLC en flux RTSP ou MJPEG (indiquer leur adresse dans `source`).

## Personnes (Module 3)

Page `/people` : liste, recherche (nom, prénom, matricule), filtre actif/inactif, ajout (nom, prénom, matricule, infos, plusieurs photos, consentement obligatoire), modification, désactivation, suppression. Fiche `/people/{id}` : informations, photos de référence (jamais en dessous de 1), activité (passages, correspondances probables / à vérifier, zones, chronologie, événements).

- **Statuts de reconnaissance** : « correspondance probable » (similarité ≥ `NIIFYEEN_DB_PROBABLE_THRESHOLD`, 0,50, sans ambiguïté), « à vérifier » (≥ `NIIFYEEN_DB_THRESHOLD`, 0,35, ou autre personne à moins de `NIIFYEEN_DB_AMBIGUITY_MARGIN`, 0,05), sinon inconnu. Ce n'est jamais une identité certaine ; les seuils sont à calibrer sur de vraies photos.
- **Doublons** : un visage proche (≥ `NIIFYEEN_PERSON_DUPLICATE_THRESHOLD`, 0,60) d'une personne existante demande confirmation ; un matricule est unique (insensible à la casse).
- **Liaison par identifiant** : les passages et événements sont liés par `participant_id`, pas par le nom (homonymes possibles). L'historique affiche le nom actuel. Au premier démarrage, les anciens passages sont rattachés uniquement si le nom est non ambigu.
- Une personne **inactive** reconnue « probable » déclenche un événement d'avertissement.
- Suppression : efface empreintes et références, anonymise passages et événements.
- Avec `NIIFYEEN_HISTORY_IDENTITIES=0`, noms et identifiants sont masqués (les fiches renvoient 403).

## Cibles : personne enrôlée, capacité, session (Module 4)

Sur la page d'accueil, le panneau « Suivre une personne déjà enrôlée » ajoute comme cible une personne de la base **sans photo** (moyenne de ses empreintes de référence). Une personne inactive peut être suivie. Une même personne n'est jamais cible deux fois (par id ni par ressemblance).

- **Capacité** : `NIIFYEEN_MAX_TARGETS` (8 par défaut, maximum 32) ou réglage à chaud (`PUT /api/targets/settings`), mémorisé en base. On ne peut pas descendre sous le nombre de cibles en cours.
- **Session** : « Sauvegarder » mémorise les **identifiants** des personnes enrôlées suivies ; « Reprendre » les remet comme cibles (nouveaux trajets). Les cibles envoyées par photo ne sont **pas** sauvegardées : on ne conserve pas l'empreinte d'une personne qui n'a pas consenti. Les cibles ne sont pas reprises automatiquement au démarrage.
- **Historique** : chaque cible reconnue propose « Fiche » et « Historique de ses passages » (`/history?participant_id=N`). Ce filtre ne s'applique qu'au tableau des passages ; la synthèse et les trajets restent globaux.
- Routes : `POST /api/targets/from-person`, `GET|PUT /api/targets/settings`, `GET|DELETE /api/targets/session`, `POST /api/targets/session/save|resume`. Avec `NIIFYEEN_HISTORY_IDENTITIES=0`, choisir une personne et reprendre une session sont refusés (403).

## Carte, étages et caméras placées (Module 5)

Page `/map` (menu « Carte ») :

- **Étages** : chaque étage a un nom, un niveau et un plan (PNG, JPEG ou SVG, 6 Mo maximum, 12 étages). Les images sont ré-encodées en PNG (métadonnées retirées, 2400 px maximum). Un SVG est refusé s'il contient du script, des gestionnaires d'événements, des objets externes, des références externes ou un `DOCTYPE`, puis servi avec une politique de sécurité qui interdit tout script. Tant qu'aucun étage n'existe, un fond neutre vide est affiché avec toutes les caméras.
- **Placement** : « Modifier le plan » permet de cliquer pour placer une caméra, de la glisser pour la déplacer et de régler sa direction (0-360°, 0 = haut), son ouverture (10-360°) et sa portée. Le secteur de vision est dessiné sur le plan. Ces réglages sont enregistrés par caméra (`floor_id`, `x`, `y`, `heading`, `fov`, `view_range`) via `PUT /api/admin/cameras/{id}`. Ce champ de vision est **indicatif** : il est réglé à la main, pas calculé depuis l'image de la caméra.
- **Cibles en direct** sur l'étage affiché (rafraîchi toutes les 3 s). Les étapes d'un trajet situées sur un autre étage ne sont pas tracées sur celui-ci.
- **Rejeu** d'un trajet enregistré (lecture, pause, curseur, vitesse ×1 à ×60) ; l'étage affiché suit la caméra courante.
- **Analyse** sur la période choisie : passages et temps cumulé par caméra (délai jusqu'à l'étape suivante ; la dernière étape n'a pas de durée), trajets les plus fréquents entre caméras.
- Supprimer un étage remet ses caméras à « non placées ». Remplacer un plan par un plan de proportions différentes déforme les positions : l'interface le signale.
- Routes : `GET|POST /api/floors`, `PUT|DELETE /api/floors/{id}`, `GET /api/floors/{id}/image`.
- Couverture : `GET /api/floors/{id}/coverage` (part du plan vue par au moins une caméra, par plusieurs, angles morts), calcul géométrique qui ignore murs et obstacles. Limites : pas de plan multi-site, positions en fractions du plan.

## Carte d'une ville (outil `tools/city_map.py`)

Le plan d'un étage n'est qu'une image : l'outil importe la carte d'Ouagadougou, crée les caméras et les place, en une commande (serveur lancé) :

```powershell
.\venv\Scripts\python.exe tools\city_map.py --sources 0,1,rtsp://ip2/flux,http://ip3:8080/video
```

- `--sources` : jusqu'à 4 sources (index de webcam, adresse RTSP ou HTTP). Les caméras `cam1`… sont créées ; si une source est déjà utilisée (la webcam 0 est enregistrée au premier démarrage), la caméra existante est placée à la place.
- Carte par défaut : `cartes-locales/ouagadougou-google.png` (capture Google Maps, positions repérées à la main : centre-ville, université Joseph Ki-Zerbo, stade du 4 Août, aéroport). Cette image est **protégée par le droit d'auteur de Google** : ignorée par Git (`cartes-locales/`), à ne pas publier ; garder le logo Google et la mention « Map data », et citer la source.
- Autre carte (OpenStreetMap, Exporter, zone manuelle) : `tools\city_map.py carte.png --bbox ouest,sud,est,nord` place les caméras par coordonnées GPS. Fond : © les contributeurs d'OpenStreetMap.
- Relancer la commande ne duplique rien (étage et caméras existants conservés). Les emplacements sont **indicatifs** : les corriger en déplaçant les caméras sur la page Carte.

Les portées sont une fraction de la largeur de l'image : sur une carte de ville, les champs de vision ne sont que des repères et le pourcentage de couverture n'a pas de sens physique.

## Fonctionnalités retirées

À la demande de ses auteurs, les pages et API suivantes ont été supprimées : **Lignes et zones** (comptage, affluence, intrusion, présence prolongée), **Statistiques**, **Rapports** PDF, **Investigation** (chronologie, dossiers), **Enregistrements** vidéo, **Journal agrégé** et le **mode démonstration**. Les tables correspondantes (`zones`, `zone_stats`, `recordings`, `dossiers`…) sont conservées dans la base : aucune donnée n'est effacée. Les anciennes alertes de ces types restent lisibles.

**Carte réelle dans « Plan et cheminement » et « Historique »** : ces pages affichent l'étage importé qui contient des caméras (par exemple la carte d'Ouagadougou, voir ci-dessous) au lieu de l'ancien plan du bâtiment, supprimé : un fond neutre vide s'affiche s'il n'existe aucun étage avec caméras.

## Interface commune (Module 13)

Toutes les pages partagent la même coque : barre latérale (réductible), thème clair / sombre mémorisé, pastille d'alertes.
Les quatre anciennes pages (Accueil devenue « Cibles et personnes », « Plan et cheminement », « Journal agrégé », « Historique ») gardent leur contenu, leurs identifiants
et leur JavaScript ; seul l'habillage change (`legacy.css`, limité à `.legacy`, sans style en ligne). Les anciens liens de navigation en haut de page sont remplacés par le menu latéral.
`layout.css` n'est plus utilisé (le fichier peut être supprimé de votre dossier).

*Vérifié : tests statiques, pages ouvertes dans Chromium (1366 px et 390 px, thèmes clair et sombre, aucune erreur JavaScript, aucun débordement horizontal), actions de l'Accueil et de l'Historique essayées. Non essayé avec Firefox ni Safari.*

## Centre d'alertes (Module 7)

**Alertes limitées aux cibles (par défaut)** : seuls les événements « Cible repérée », « Cible passée d'une caméra à une autre » et « Cible perdue de vue » créent une alerte. Les autres (caméra hors ligne, intrusion, affluence, personne reconnue…) restent dans le journal et l'investigation, sans alerte, sans son et sans notification externe. La case « Alertes uniquement pour les cibles » de la page Alertes (ou `PUT /api/alerts/scope`) rétablit le fonctionnement complet ; les règles déjà réglées sont conservées dans les deux cas.

Page `/alerts` (menu « Alertes », avec une pastille du nombre d'alertes nouvelles sur toutes les pages). Une **alerte** est un événement de supervision qui demande l'attention de l'opérateur ; elle a un identifiant, une catégorie (cibles, personnes, caméras, lignes et zones), une priorité (faible, moyenne, haute, critique), un horodatage, la caméra ou la zone concernée, une description et un **état** :

`nouvelle` → `consultée` → `acquittée` → `clôturée` (une alerte clôturée peut être rouverte). On avance, on ne recule pas ; on peut sauter des étapes. Ouvrir une alerte nouvelle la marque « consultée ». Chaque changement, chaque note et chaque notification est conservé dans l'**historique** de l'alerte. Recherche texte, filtres (état, priorité, catégorie, caméra, période) et actions par lot sont disponibles.

- **Règles** (par type d'événement, modifiables dans la page) : actif ou non, priorité, **délai de regroupement**, son, notification externe. Les événements identiques (même type, même caméra, même zone/cible/personne) arrivant dans ce délai alimentent **une seule alerte** (compteur « ×N ») au lieu d'en créer une par événement. Par défaut sont actifs : cible repérée/déplacée/perdue, personne désactivée reconnue, caméra hors ligne/masquée/figée, seuil de fréquentation dépassé, intrusion en zone interdite. Sont **désactivés** (très fréquents) : franchissement de ligne et simple reconnaissance d'une personne.
- **Notifications dans l'application** : toast et son dans toutes les pages (interrupteur « Son » sur la page Alertes ; un navigateur peut bloquer le son tant qu'on n'a pas cliqué dans la page).
- **Notifications externes (facultatives)** : e-mail (SMTP) et Telegram, configurés **uniquement par variables d'environnement** (jamais en base, jamais renvoyés par l'API) :
  `NIIFYEEN_SMTP_HOST`, `NIIFYEEN_SMTP_PORT` (587), `NIIFYEEN_SMTP_USER`, `NIIFYEEN_SMTP_PASSWORD`, `NIIFYEEN_SMTP_FROM`, `NIIFYEEN_SMTP_TO` (liste séparée par des virgules), `NIIFYEEN_SMTP_SECURITY` (`starttls` par défaut, `ssl` ou `none`) ; `NIIFYEEN_TELEGRAM_TOKEN`, `NIIFYEEN_TELEGRAM_CHAT_ID` ; `NIIFYEEN_NOTIFY_MIN_PRIORITY` (`high` par défaut), `NIIFYEEN_NOTIFY_PER_MINUTE` (10), `NIIFYEEN_NOTIFY_IDENTITIES` (`0` par défaut). Les envois partent dans un thread à part (ils ne ralentissent pas l'analyse), sont plafonnés par minute, et leur résultat (envoyée / échec / limité) est inscrit dans l'alerte. **Par défaut les messages ne contiennent aucune identité** (type, caméra, heure, priorité seulement) ; les envoyer à un service externe est une décision de l'exploitant. Un bouton d'essai par canal est proposé sur la page.
- **Vie privée** : les alertes sont purgées avec la rétention (`NIIFYEEN_RETENTION_DAYS`) ; supprimer une personne efface son nom des alertes qui la concernent.
- Routes : `GET /api/alerts` (filtres, `before_id` / `after_id`), `GET /api/alerts/{id}`, `POST /api/alerts/{id}/status|note`, `POST /api/alerts/bulk`, `GET|PUT /api/alerts/rules`, `GET /api/alerts/channels`, `POST /api/alerts/channels/test`, `GET /api/alerts/summary`.

Limites : seuls les événements **émis après le démarrage de ce module** deviennent des alertes. Pas de comptes utilisateurs : une alerte n'est pas attribuée à un opérateur (l'application n'a pas d'authentification, par choix). L'envoi e-mail/Telegram a été testé avec des **faux** serveurs d'envoi, pas avec un vrai compte SMTP ni un vrai bot ; Telegram exige un accès Internet sortant. Les titres des alertes affichés dans l'application peuvent contenir le nom d'une personne reconnue (comme le fil de supervision).

## Cibles multiples

Plusieurs cibles peuvent être suivies **en même temps** (8 au maximum). Chaque envoi de photos **ajoute** une cible : les précédentes, et leurs photos, restent en place.

- Chaque cible a un libellé (« Cible 1 », « Cible 2 »…, la numérotation repart de 1 quand on retire toutes les cibles), une **couleur** (cadre vidéo, liste, plan, trajet), sa **photo**, son propre suivi de trajet et sa propre fiche.
- Les caméras comparent chaque visage à **toutes** les cibles ; la plus proche au-dessus de son seuil l’emporte. Les détections portent l’identifiant de la cible reconnue.
- La liste des cibles (accueil et page `/cameras`) permet de **retirer une cible** sans toucher aux autres, ou toutes d’un coup. La page caméras indique en direct où se trouve chaque cible.
- Ajouter une photo de quelqu’un qui est déjà cible est refusé (« Cette personne est déjà suivie (Cible 1) ») ; une photo sans visage exploitable n’efface aucune cible.
- Les alertes sonores et visuelles sont déclenchées **par cible** (première détection, ou changement de caméra).

Limites :

- Les cibles et leurs **photos** sont **en mémoire** : elles disparaissent au redémarrage du serveur (les trajets déjà enregistrés restent dans l’historique). Les photos ne sont jamais écrites sur disque ni en base ; seule une vignette réduite (200 px) est conservée, et elle est servie sans authentification par `/targets/{id}/photo`.
- Plus il y a de cibles, plus le risque de **confusion entre deux personnes qui se ressemblent** augmente : une personne peut être attribuée à la mauvaise cible. Aucun seuil n’a été validé sur des données réelles.
- Le suivi des cibles s’appuie sur le nom affiché « Cible N » : les passages « cible » de l’historique portent ce libellé, pas le nom d’une personne enrôlée.

## Fiche de la cible

Quand une cible est ajoutée (recherche par photo), le système compare son empreinte faciale à celles des personnes enrôlées. Si l’une d’elles est assez proche, la **fiche** de cette personne s’affiche **dans la carte de la cible** (rubrique « Fiche », ouverte sur l’accueil, repliée sur `/cameras`), tant que la cible est suivie. Chaque cible a sa propre fiche. Les cibles réapparaissent à l’ouverture des pages.

Contenu de la fiche :

- **Fiche d’enrôlement** : identifiant, nom, informations saisies, date de consentement ;
- **Empreintes** : nombre d’empreintes faciales enregistrées et noms des fichiers photo d’origine (les empreintes elles-mêmes ne sont jamais envoyées au navigateur) ;
- **Détections** : nombre de passages et d’observations enregistrés sous son nom, première et dernière détection, dernière caméra, détail par caméra ;
- **Qualité de la correspondance** : similarité et seuil, et les autres personnes enrôlées qui dépassent aussi le seuil (avertissement de vérification manuelle).

Si personne ne correspond, la page l’indique (avec la similarité la plus proche, sans nommer personne) et la cible reste suivie. Si la personne est supprimée après la recherche, la fiche le signale.

Le seuil d’affichage (`NIIFYEEN_PROFILE_THRESHOLD`) est par défaut celui de la cible (0,40) : afficher des données personnelles demande au moins autant de confiance qu’un suivi. Il n’a pas été validé sur des données réelles.

Limites et précautions :

- **Une correspondance est une estimation.** Un faux positif affiche la fiche d’une autre personne : vérifier avant toute décision. Les cas où plusieurs personnes sont proches sont signalés, mais pas ceux où la personne recherchée n’est pas enrôlée et ressemble à quelqu’un qui l’est.
- Les détections sont retrouvées **par le nom** (elles ne stockent pas l’identifiant de la personne) : deux personnes de même nom sont confondues. Pendant qu’une personne est *la cible*, ses passages sont enregistrés comme « cible » et non sous son nom ; ils ne sont donc pas comptés dans la fiche.
- **La fiche est accessible sans authentification** à quiconque peut envoyer une photo au serveur : elle permet de retrouver les informations d’une personne enrôlée à partir de son visage. Ne pas exposer le serveur hors de `127.0.0.1`. Avec `NIIFYEEN_HISTORY_IDENTITIES=0`, la fiche indique seulement qu’une personne enrôlée est reconnue, sans nom ni informations.
- Ne comparer à la base que des personnes ayant donné leur consentement à être enrôlées : c’est la condition posée à l’enrôlement.

## Historique des détections

La page `/history` répond à « qui est passé devant quelle caméra, quand et combien de temps ».

**Observations et passages.** La base reçoit une *observation* environ toutes les 0,8 s par personne visible. L’historique les regroupe en **passages** : les observations d’une même piste sur une même caméra forment un seul passage tant qu’elles sont espacées de moins de 10 s. Son type est le plus fort rencontré pendant le passage (cible, puis personne enrôlée, puis inconnu), ce qui évite qu’une personne d’abord vue comme « inconnu » puis reconnue produise deux lignes. Sur un jeu réel de 4 527 observations, on obtient environ 50 passages.

**Filtres** : période (de 10 minutes à 30 jours, ou dates libres jusqu’à 90 jours), caméra, type et nom. Une actualisation automatique toutes les 10 s est disponible pour les périodes relatives.

**Synthèse** : nombre de passages et d’observations, répartition par type et par caméra, courbe d’activité (la part de la cible est en rouge).

**Export CSV** : tous les passages correspondant aux filtres (50 000 lignes maximum), séparateur `;`, UTF-8 avec BOM (s’ouvre correctement dans Excel en français), dates en UTC. Les cellules commençant par `=`, `+`, `-` ou `@` sont préfixées pour empêcher l’exécution de formules dans le tableur.

**Trajets des cibles.** Chaque cible ouvre sa propre *session* ; sa première identification et chacun de ses changements de caméra sont enregistrés en base. Ils survivent au redémarrage du serveur et se rejouent sur le plan : on peut sélectionner **plusieurs trajets** pour les comparer, chacun dans sa couleur. Retirer une cible arrête son enregistrement mais conserve son historique.

Limites et précautions :

- Un passage n’est **pas** une personne unique : une même personne qui sort puis revient, ou dont la piste est perdue puis recréée par le suivi, donne plusieurs passages. Les identifiants de piste repartent de zéro au redémarrage du serveur.
- L’historique reflète la reconnaissance, avec ses erreurs : un passage « cible » ou « personne enrôlée » peut être un faux positif (voir les seuils dans [Configuration](#configuration)).
- Le regroupement utilise les fonctions de fenêtre SQLite : SQLite 3.25 ou plus est requis (la version fournie avec Python 3.10 convient).
- **L’historique affiche les noms des personnes enrôlées et n’est protégé par aucune authentification** (comme le reste de l’application). Pour afficher « Personne enrôlée » à la place des noms dans l’historique, l’export et `/detections/recent`, démarrer avec `NIIFYEEN_HISTORY_IDENTITIES=0` ; la recherche par nom est alors désactivée. Le journal `/logs` ne montre jamais d’identité.
- Les passages se calculent à la demande sur les observations conservées : au-delà de la durée de rétention, ils disparaissent.

## Données personnelles et historique Git

Les anciennes versions du dépôt contenaient `niifyeen.db`, `backups/`, `.idea/`, `yolov8n.pt` et des fichiers `.pyc`. Ils sont désormais ignorés, mais **restent dans l’historique Git** tant qu’il n’est pas réécrit. La base contenait des noms, des empreintes faciales et des observations horodatées.

Pour les retirer de tout l’historique, sur une **copie neuve** du dépôt (la réécriture est destructive) :

```powershell
pip install git-filter-repo
git clone <url-du-depot> niifyeen-propre
Set-Location niifyeen-propre
git filter-repo --invert-paths --path niifyeen.db --path backups --path .idea --path yolov8n.pt --path-glob "*.pyc" --path-glob "*.db"
git remote add origin <url-du-depot>
git push --force --all
```

Ensuite :

- Demander aux autres personnes qui ont cloné le dépôt de le recloner (ne pas fusionner l’ancien historique).
- Si le dépôt a été public, ne pas se fier à cette réécriture : les copies, forks et caches de GitHub peuvent subsister. Considérer les données comme exposées, informer les personnes enrôlées et, si besoin, demander à GitHub de purger les caches.
- Conserver une sauvegarde de la base **hors** du dépôt.

Pour un usage sur des personnes réelles, le cadre légal (en France / UE : RGPD, données biométriques, analyse d’impact, information des personnes) reste à traiter ; ce prototype n’en tient pas lieu.

## API

### Consultation et transport

| Méthode | Route | Description |
| --- | --- | --- |
| GET | `/` | Page d’accueil |
| GET | `/cameras` | Tableau de bord vidéo |
| GET | `/api/cameras` | Liste des identifiants et métadonnées de carte |
| GET | `/video/{cam_id}` | Flux MJPEG ; 404 si caméra inconnue |
| GET | `/detections/recent` | Observations récentes en JSON |
| GET | `/participants` | Personnes enrôlées (id, nom, date de consentement) |
| GET | `/supervision` | Page du centre de supervision |
| GET | `/admin/cameras` | Page de gestion des caméras |
| GET / POST | `/api/admin/cameras` | Liste (toutes, y compris désactivées) / création |
| PUT / DELETE | `/api/admin/cameras/{id}` | Modification partielle / suppression |
| POST | `/api/admin/cameras/{id}/restart` | Relance d'un flux |
| POST | `/api/admin/cameras/test` | Test de connexion (source saisie, ou source enregistrée d'une caméra) |
| GET | `/api/admin/cameras/webcams` | Webcams disponibles sur le serveur |
| POST | `/api/admin/cameras/discover` | Découverte ONVIF (WS-Discovery) |
| GET | `/api/supervision/cameras` | Caméras (identité, lieu, état, FPS, personnes, source **masquée**), cibles, personnes visibles |
| GET | `/api/events` | Événements de supervision (`limit`, `after_id`, `before_id`, `family`, `camera_id`, `severity`) |
| WS | `/ws/video` | Images JPEG de toutes les caméras demandées sur une seule connexion (voir ci-dessous) |
| GET | `/targets` | Cibles en cours (libellé, couleur, photo, fiche), reconstruites à chaque appel |
| GET | `/targets/{id}/photo` | Vignette de la cible (en mémoire, jamais enregistrée) ; 404 si inconnue |
| GET | `/history` | Page de l’historique des détections |
| GET | `/api/history/episodes` | Passages (filtres, pagination) |
| GET | `/api/history/episodes.csv` | Export CSV des passages |
| GET | `/api/history/summary` | Synthèse et courbe d’activité |
| GET | `/api/history/trajectories` | Trajets enregistrés des cibles |
| WebSocket | `/ws/live` | Événements et états en direct |

`/detections/recent` accepte `minutes` entre 1 et 240, avec une valeur par défaut de 10. Le résultat contient au maximum 800 observations, des plus récentes aux plus anciennes. Il n’y a pas encore de pagination ni de filtre par caméra sur cette route.

Exemple de lecture de l’historique lorsque le serveur est démarré :

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/detections/recent?minutes=10'
```

### Autres routes présentes

Routes d’écriture (module biométrique) :

| Méthode | Route | Description |
| --- | --- | --- |
| POST | `/participants/enroll` | Champs : `nom`, `infos`, `consentement=true` (obligatoire), 1 à 10 `photos` de 5 Mo maximum. 422 si aucun visage exploitable (aucune fiche n’est créée) |
| DELETE | `/participants/{id}` | Supprime la personne et ses empreintes ; 404 si inconnue |
| POST | `/search/by-image` | **Ajoute** une cible à partir de 1 à 10 photos (les suivantes sont ignorées) ; les cibles déjà définies sont conservées. 409 si la personne est déjà une cible ou si le maximum est atteint. La réponse contient la `profile` de la cible (voir [Fiche de la cible](#fiche-de-la-cible)) |
| DELETE | `/targets/{id}` | Retire une seule cible ; 404 si inconnue |
| POST | `/target/clear` | Retire toutes les cibles |

Erreurs de validation : 400 (consentement absent, trop de photos), 413 (fichier trop volumineux), 422 (nom vide ou trop long, aucun visage).

## Tests

Depuis la racine (installer `requirements-dev.txt` suffit) :

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -t . -v
```

Les 516 tests (dont l’API, qui exige `httpx2` ou `httpx` : voir `requirements-dev.txt`) couvrent : la diffusion MJPEG, le suivi du parcours de la cible (confirmation de changement de caméra, expiration, plafond), la ré-identification, la validation de `cameras.json`, la base (migration d’une ancienne base, purge, suppression d’une personne), le logger, le cycle de vie d’une caméra (reconnexion, arrêt propre), l’API HTTP/WebSocket (consentement, limites d’envoi, suppression), la fiche de la cible (correspondance, seuil, personne supprimée, identités masquées), l’historique (passages, filtres, synthèse, export, masquage des identités, trajets enregistrés), la logique du tracé sur le plan (exécutée avec Node.js, ignorée s’il est absent) et l’absence de `innerHTML` dans les pages.

Ils utilisent des objets simulés : YOLO et InsightFace ne sont pas chargés, aucune caméra n’est ouverte et la base de test est un fichier temporaire. Ils ne valident ni la précision de détection, ni la reconnaissance, ni le fonctionnement sur les caméras du site. Un workflow GitHub Actions (`.github/workflows/tests.yml`) les exécute à chaque push.

## Dépannage

| Symptôme | Vérification ou action |
| --- | --- |
| `No module named uvicorn` ou autre dépendance | Utiliser l’interpréteur du bon environnement et vérifier ses dépendances ; éviter de mélanger Python global et `venv`. |
| Le Python de `venv` ne démarre plus | L’environnement peut dépendre d’une installation Python déplacée. Recréer un environnement à partir d’une liste de dépendances validée. |
| Port 8000 occupé | Arrêter l’ancien serveur ou utiliser `--port 8001`, puis ouvrir l’adresse correspondante. |
| Webcam occupée / pas de signal | Fermer les autres applications utilisant la caméra, vérifier les permissions Windows et la connexion physique. |
| Caméra réseau inaccessible | Vérifier le réseau local et l’accessibilité de sa source vidéo depuis la machine serveur. |
| Interface toujours ancienne | Effectuer un rechargement complet avec Ctrl + F5. |
| Pas de signal sonore | Interagir avec la page et vérifier le volume ainsi que les restrictions audio du navigateur. |
| Erreur de colonne SQLite | Arrêter le serveur, sauvegarder la base et examiner la nécessité d’une migration. |
| Historique vide malgré des événements | Consulter la console du serveur : les erreurs d’écriture en base y sont maintenant journalisées. |
| Seule la webcam 0 apparaît | Premier démarrage sans `cameras.json` : ajouter les autres caméras dans `/admin/cameras`. |
| Le serveur refuse de démarrer avec un message sur `cameras.json` | Corriger le fichier, ou le renommer : il n'est lu qu'au tout premier démarrage (avant l'import). |
| Chargement initial lent | Les modèles sont chargés à la demande ou au démarrage ; des ressources manquantes peuvent déclencher des téléchargements. |
| Images lentes malgré la connexion | Distinguer le temps d’analyse, le transport réseau et le rendu du navigateur ; la limite d’envoi MJPEG ne mesure pas le débit réel du détecteur. |

## Limites

Le projet reste un prototype local :

- **Pas d’authentification ni de rôles** (choix assumé à ce stade) : toutes les routes, y compris la liste des personnes enrôlées, la suppression et le flux vidéo, sont ouvertes à quiconque atteint le serveur. Le démarrage proposé écoute uniquement sur `127.0.0.1` ; ne jamais utiliser `--host 0.0.0.0` sans ajouter une authentification ou un proxy qui la fournit.
- Pas de gestionnaire de caméras dans l’interface.
- Une caméra réseau qui se fige sans fermer la connexion peut bloquer la lecture ; le flux n’est rouvert que lorsque la lecture échoue.
- Les tâches d’analyse et les accès à la base peuvent ralentir le serveur selon la machine.
- Les représentations faciales et certains états sont partagés par le processus ; le système n’est pas conçu pour plusieurs sessions de recherche indépendantes.
- Pas de tests d’intégration complets, de mesures de précision ou de benchmark multi-caméras documentés.
- Migration limitée aux colonnes de `detections` ; pas de journal d’audit des accès.
- Le consentement est une case à cocher : il n’y a pas de recueil formel, de justificatif ni de retrait par la personne elle-même.

Les flux et la base peuvent contenir des données personnelles. L’accès, la sauvegarde et la durée de conservation doivent être définis avant tout usage réel. Une exposition publique n’est pas couverte par la configuration actuelle.

## État de cette documentation

Rédigée à partir des sources et des métadonnées de l’environnement local le **20 octobre 2026**. La rédaction ne change ni les fonctions de l’application, ni les sources vidéo, ni les données. La procédure d’installation sur une machine vierge et le fonctionnement matériel des caméras restent à valider séparément.


## Plan du site : caméra d’identification et trajet des cibles

Quand des cibles ont été ajoutées (recherche par photo) et qu’elles sont reconnues, la page `/cameras` les indique sur le plan, **chacune dans sa couleur** :

- **Caméra d’identification** : la caméra où une cible est vue prend la couleur de cette cible, avec un anneau qui pulse et la mention « Cible N ici » (une ligne par cible présente sur la même caméra). Une ligne de statut au-dessus du plan donne le nom de la caméra et le score.
- **Trajet** : une flèche relie chaque caméra à la suivante, dans l’ordre des passages. Des pastilles numérotées de la couleur de chaque cible indiquent l’ordre (une caméra revisitée porte plusieurs numéros, par exemple « 1 · 3 »). Les flèches les plus anciennes sont plus pâles ; survoler une flèche affiche l’étape et l’heure. Seuls les 20 derniers déplacements sont dessinés ; la numérotation reste continue.
- **Cible perdue** : au bout de 3 secondes sans détection, la dernière caméra est entourée en pointillé avec « Cible N : dernière vue », et le trajet reste affiché jusqu’au retrait de la cible. Les trajets de deux cibles sur le même couloir sont légèrement écartés pour rester lisibles.

Les caméras sont placées d’après leurs réglages `x` et `y` (entre 0 et 1 : fraction de la largeur et de la hauteur du plan), modifiables dans `/admin/cameras` (réglages avancés). Sans ces champs, les trois premières caméras prennent les emplacements dessinés sur le plan (entrée, salle, sortie) et les suivantes sont alignées le long du couloir. Pour adapter le plan à votre bâtiment, importer une carte comme étage (page Carte) et ajuster `x` et `y`.

Limites :

- Le plan est schématique et sans échelle. Les flèches sont des courbes entre deux caméras ; elles ne représentent **pas** le chemin réellement parcouru (elles peuvent traverser des murs) et ne donnent pas la position de la personne entre deux caméras.
- Un changement de caméra n’est enregistré qu’après 0,8 s de présence continue sur la nouvelle caméra : un passage plus bref n’apparaît pas.
- Le trajet est en mémoire : il est perdu au redémarrage du serveur et n’est pas relu depuis la base.
- Une personne confondue avec la cible produit un trajet faux : l’affichage reflète la reconnaissance, avec ses erreurs possibles (voir les seuils dans [Configuration](#configuration)).

## Envoi des alertes de cible aux autorités

Page **Autorités** (`/authorities`). Chaque autorité a un nom, un service, un téléphone, des caméras rattachées, une position sur la carte et un **lien secret** vers sa boîte de réception. Quand une cible est repérée (`target_seen`) ou passe d'une caméra à une autre (`target_moved`), le message est adressé à l'autorité rattachée à la caméra ; à défaut, à la plus proche sur la carte (distance sur le plan, autorités et caméras positionnées).

**Boîte de réception interne (canal par défaut, sans service externe).** Un second petit serveur (`backend/app/inbox.py`, port `NIIFYEEN_INBOX_PORT`, 8090 par défaut) sert uniquement les pages de réception : aucune autre route de l'application n'y est accessible. L'autorité ouvre son lien sur son téléphone ou son ordinateur, voit les alertes (la plus récente en haut, mise à jour toutes les 3 s, vibration sur les nouveautés) et appuie sur « Pris en charge ». Elle ne voit que ses propres messages. Le lien contient un secret de 32 caractères ; « Nouveau lien » invalide l'ancien aussitôt.

- **Ouvrir la réception au réseau local** : par défaut elle n'écoute que sur l'ordinateur (`127.0.0.1`). Trois façons de l'ouvrir aux téléphones : la case **« Ouvrir la boîte de réception aux téléphones du réseau local »** de la page Autorités (réglage enregistré dans la base, la réception se relance seule), `lancer.bat` / `lancer.ps1` à la racine, ou `$env:NIIFYEEN_INBOX_HOST="0.0.0.0"` avant de lancer le serveur (la case est alors verrouillée). Il faut aussi autoriser le port 8090 dans le pare-feu Windows, **y compris pour un réseau classé « Public »** (`New-NetFirewallRule -DisplayName "NiifYeen inbox" -Direction Inbound -Protocol TCP -LocalPort 8090 -Action Allow -Profile Any`). Le lien utilise l'adresse de l'ordinateur sur le réseau ; quand il y a plusieurs cartes réseau (VirtualBox, WSL), les adresses utiles sont toutes listées sous le tableau et celles de VirtualBox ou auto-attribuées (`192.168.56.x`, `169.254.x.x`) sont écartées. Certains réseaux isolent les appareils entre eux : utiliser alors le partage de connexion d'un téléphone. L'application principale reste sur `127.0.0.1`.
- **Suppression** : « Supprimer » (par alerte) et « Tout supprimer » retirent les alertes de la boîte de l'autorité et effacent leurs photos en mémoire. Le journal de la page Autorités les conserve (colonne `hidden` de la table `dispatches`) : une autorité ne peut pas effacer les traces de l'envoi. Une autorité ne peut supprimer que ses propres alertes.
- **Sécurité** : le lien secret est le seul contrôle d'accès et la connexion est en HTTP non chiffré. À utiliser sur un réseau isolé ou de confiance (point d'accès dédié, pas le Wi-Fi public). Qui possède le lien lit les alertes de cette autorité, y compris l'identité des personnes reconnues : ne pas le diffuser. Pas de HTTPS ni d'authentification par compte dans ce prototype.
- **Contenu du message** : cible, lieu, heure, confiance du système, caméra précédente, fiche de la personne enrôlée si elle est reconnue (nom, référence, informations, fiche active, score), derniers passages. **Les empreintes faciales ne sont jamais transmises.**
- **Photos** : uniquement des **images prises par la caméra à l'instant de la détection**, jamais la photo envoyée pour définir la cible. D'abord un **gros plan de la personne détectée** (découpé dans l'image de la caméra, sans cadre ni texte, rafraîchi chaque seconde ; ignoré s'il a plus de 3 s ; légèrement agrandi pour la lisibilité sur téléphone, sans information ajoutée), puis l'**image de la caméra** avec la cible encadrée. Les photos ne sont gardées qu'**en mémoire** (60 envois, 1 heure) : ni disque ni base ; après un redémarrage ou une heure, le texte reste, la photo disparaît. `NIIFYEEN_DISPATCH_PHOTO=0` pour ne rien joindre.
- **Page de réception** : onglets « À traiter » / « Toutes », compteur, indicateur « En direct », carte par alerte (cible, caméra et lieu, personne reconnue, confiance, « il y a… »), galerie de photos à faire défiler et à agrandir, détails complets repliables, bouton « Pris en charge », **alarme sonore** à chaque nouvelle alerte (activée par défaut, désactivable, bouton « Tester »), rejouée toutes les 15 s tant qu'une alerte n'est pas prise en charge, et vibration. Les navigateurs interdisent le son avant un premier geste : une bannière « Touchez ici pour activer le son » le demande à chaque ouverture de la page. Le son ne retentit que si la page reste ouverte (écran allumé, volume monté) : un téléphone verrouillé ou la page en arrière-plan peuvent le suspendre, et les notifications du système exigent HTTPS, non fourni, thème clair/sombre automatique.
- **Application Android** (`appli/`, Flutter) : alternative à la page web pour les téléphones Android. Elle enregistre le lien de l'autorité, interroge `/a/{jeton}/api/messages` toutes les 3 s dans un service d'avant-plan (`flutter_foreground_task`) et notifie chaque nouvelle alerte par une notification sonore (`flutter_local_notifications`). La notification ne contient que la cible, la caméra, le lieu et l'heure, sans photo. Écrans : liste (à traiter / toutes, photo, identité, confiance), fiche de la cible (photos, identité, détection, derniers passages), prise en charge, suppression. L'historique existant n'est pas notifié à la première connexion. Autorisations demandées : notifications et exemption d'optimisation de batterie. Trafic HTTP en clair autorisé (réseau local). Les termes du message d'alerte sont relus par `appli/lib/details.dart` : si le format de `compose_message` change, adapter ce fichier (le texte non reconnu est affiché tel quel).
- **Telegram (facultatif)** : si `NIIFYEEN_TELEGRAM_TOKEN` et l'identifiant Telegram d'une autorité sont renseignés, le message lui est aussi envoyé par Telegram. C'est un service externe : à éviter si les données doivent rester internes.
- **Garde-fous** : un seul envoi par couple cible/caméra et par délai (`NIIFYEEN_DISPATCH_COOLDOWN_SEC`, 120 s), interrupteur général sur la page, journal complet (autorité, cible, caméra, canal, statut, texte exact), message d'essai sans donnée personnelle, désactivation possible de la réception (`NIIFYEEN_INBOX_ENABLED=0`).
- **Limites** : pas de SMS ; HTTP non chiffré ; l'application Android n'a pas été validée dans la durée (certains constructeurs coupent les services d'arrière-plan), pas de version iPhone ; une correspondance d'identité est une estimation et peut être fausse.
