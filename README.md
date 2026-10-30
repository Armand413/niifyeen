# NiifYéén

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

### 3. Configurer les caméras

Copier `cameras.example.json` vers `cameras.json` (ignoré par Git) et y indiquer les sources :

```powershell
Copy-Item cameras.example.json cameras.json
```

Sans ce fichier, seule la webcam d’index `0` (identifiant `entre`) est utilisée.

### 4. Démarrer le serveur

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

**Le démarrage ouvre les caméras configurées.** Le processus crée ou met à niveau les tables SQLite, démarre le suivi de la cible et l’enregistrement des observations, puis supprime périodiquement les observations plus anciennes que la durée de conservation (30 jours par défaut).

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
- Une carte schématique des points caméras.
- Un tableau d’événements en direct et un historique de parcours en mémoire.
- Des notifications visuelles et un signal sonore, que le navigateur peut bloquer tant que l’utilisateur n’a pas interagi avec la page.

L’état « WS connecté » confirme la connexion au serveur, **pas** le bon fonctionnement de chaque caméra. Le tableau de parcours affiché n’est pas la liste complète des détections stockées en base.

`layout.css` apporte la mise en page commune, l’adaptation aux petits écrans et les zones de tableaux défilantes. Il n’y a ni framework frontend ni étape de compilation JavaScript.

## Configuration

Tout est centralisé dans `backend/app/config.py`. Les valeurs peuvent être surchargées par des variables d’environnement ; aucun fichier `.env` n’est lu automatiquement.

| Élément | Où / comment |
| --- | --- |
| Sources et identifiants des caméras | `cameras.json` (modèle : `cameras.example.json`). Champs : `id`, `source` (index ou URL), et facultativement `label`, `x`, `y` |
| Base de données | `NIIFYEEN_DB` (par défaut `niifyeen.db` à la racine du projet) |
| Dossier des modèles InsightFace | `NIIFYEEN_MODELS_DIR` |
| Seuils de similarité | `NIIFYEEN_DB_THRESHOLD` (0,35), `NIIFYEEN_TARGET_THRESHOLD` (0,40), `NIIFYEEN_REID_THRESHOLD` (0,45) |
| Conservation des observations | `NIIFYEEN_RETENTION_DAYS` (30 ; `0` = illimitée) |
| Taille maximale d’une photo envoyée | `NIIFYEEN_MAX_UPLOAD_BYTES` (5 Mo) |
| Traitement et dimensions des images | `backend/app/person_pipeline.py` et son instanciation dans `main.py` |
| Diffusion MJPEG | `backend/app/video_stream.py` |
| Présentation web | `backend/app/static/` |

Les chemins sont **absolus** (calculés depuis la racine du projet) : le serveur peut être lancé depuis n’importe quel dossier. Un fichier `cameras.json` invalide empêche le démarrage avec un message explicite (identifiant dupliqué ou contenant des caractères interdits, source manquante…).

Les seuils de similarité n’ont pas été validés sur un jeu de test. Dans un système qui désigne une personne, un seuil trop bas produit des faux positifs : à calibrer avec des données réelles avant tout usage.

Les identifiants de caméra de `cameras.json` sont les mêmes pour les flux, le journal et les métadonnées d’affichage (`camera_meta`) : l’ancienne incohérence `entre/salle/sortie` contre `cam0/cam1/cam2` n’existe plus.

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
│   ├── target_path.py          # Parcours de la cible (thread dédié, testable)
│   ├── db_logger.py            # Journalisation périodique + rétention
│   ├── video_stream.py         # Envoi asynchrone des images MJPEG
│   ├── vision.py               # InsightFace (chargé à la demande), similarité
│   ├── reid.py                 # Rapprochement inter-caméras
│   ├── db.py                   # Modèles SQLAlchemy, migration, purge, suppression
│   ├── activity_log.py         # Agrégats par minute
│   ├── list_cams.py            # Utilitaire Windows de sondage des webcams
│   └── static/                 # index.html, cameras.html, logs.html, layout.css, site-plan.svg
└── tests/                      # 50 tests, sans caméra ni modèle
```

### Circulation des données

1. FastAPI démarre un `PersonPipeline` par caméra, dans un thread distinct.
2. OpenCV lit les images ; les modèles existants produisent les événements et les annotations.
3. Le circuit conserve la dernière image JPEG et les derniers événements en mémoire.
4. Les routes vidéo transmettent les images au navigateur ; le WebSocket transmet les événements environ toutes les 100 ms.
5. Un thread dédié (`TargetWorker`) met à jour le parcours de la cible environ 10 fois par seconde, **que le navigateur soit ouvert ou non** ; le WebSocket ne fait que lire cet état.
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

Les modes existants sont `TARGET`, `DB` et `UNK`. Un identifiant de piste appartient à une caméra ; les identifiants globaux du module existant sont temporaires et en mémoire. Aucun de ces champs ne garantit une identité exacte.

Points importants :

- Les observations sont persistées, pas les vidéos complètes.
- Les coordonnées `x` et `y` sont normalisées dans l’image, pas des coordonnées géographiques.
- Les dates sont stockées en UTC et sérialisées avec le suffixe `Z`.
- Le parcours courant et les états de suivi en mémoire sont perdus au redémarrage.
- Les photos d’enrôlement ne sont **pas** conservées : seule une empreinte numérique (embedding) et le nom du fichier source sont enregistrés. Ces empreintes restent des données biométriques.
- L’enrôlement exige une case de consentement ; la date de consentement est enregistrée. Il n’y a pas de justificatif de consentement stocké.
- Supprimer une personne (interface ou `DELETE /participants/{id}`) efface ses empreintes et remplace son nom par « inconnu » dans les observations déjà enregistrées.
- Les observations sont supprimées automatiquement après `NIIFYEEN_RETENTION_DAYS` jours (30 par défaut).

### Sauvegarde

Arrêter le serveur, puis sauvegarder `niifyeen.db` dans un emplacement protégé. Si des fichiers SQLite annexes existent, les préserver également avec la sauvegarde. Ne pas supprimer la base pour résoudre une erreur de schéma sans sauvegarde préalable.

Au démarrage, `init_db()` crée les tables manquantes, ajoute les colonnes manquantes de `detections` (migration simple pour SQLite) et crée un index sur la date. Cette migration ne couvre que les colonnes de `detections` ; sauvegarder la base avant tout changement de schéma plus important.

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
| POST | `/search/by-image` | Définit la cible à partir de 1 à 10 photos (les suivantes sont ignorées) |
| POST | `/target/clear` | Efface la cible et son parcours |

Erreurs de validation : 400 (consentement absent, trop de photos), 413 (fichier trop volumineux), 422 (nom vide ou trop long, aucun visage).

## Tests

Depuis la racine (installer `requirements-dev.txt` suffit) :

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -t . -v
```

Les 50 tests couvrent : la diffusion MJPEG, le suivi du parcours de la cible (confirmation de changement de caméra, expiration, plafond), la ré-identification, la validation de `cameras.json`, la base (migration d’une ancienne base, purge, suppression d’une personne), le logger, le cycle de vie d’une caméra (reconnexion, arrêt propre), l’API HTTP/WebSocket (consentement, limites d’envoi, suppression) et l’absence de `innerHTML` dans les pages.

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
| Seule la webcam 0 apparaît | `cameras.json` est absent : copier `cameras.example.json` (voir Lancement rapide). |
| Le serveur refuse de démarrer avec un message sur `cameras.json` | Corriger le fichier (id unique, lettres/chiffres/`-`/`_`, source renseignée). |
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


## Plan illustratif du site (30 octobre 2026)

La zone située sous les caméras affiche désormais `backend/app/static/site-plan.svg`, un dessin original local : accueil, salle polyvalente, bureau, local technique et couloir. Le bouton « Agrandir le plan » ouvre le dessin seul. Il fonctionne sans service cartographique ni connexion externe.

Les trois repères sont des emplacements proposés, fixes. Le plan ne représente pas un relevé réel, ne comporte pas d’échelle et n’affiche ni position de personne, ni trajet, ni état de connexion. L’ancien rendu sur canvas de cette zone a été remplacé ; les métadonnées de caméras du serveur (`camera_meta`) ne pilotent pas ce dessin.
