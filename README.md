# NiifYéén

**Plateforme web de supervision vidéo multi-caméras** : détection de personnes, suivi d'une cible d'une caméra à l'autre, carte de ville avec les caméras, alertes sur les cibles. Le tout dans une interface en français, sans dépendance cloud.

Projet réalisé en Python (FastAPI) lors d'un hackathon, puis structuré en 14 modules. Les captures ci-dessous ont été prises avec des **caméras simulées** (silhouettes sans visage, personnes fictives) dans une version précédente du projet : elles montrent aussi des pages retirées depuis.

![Centre de supervision](docs/screenshots/supervision.png)

## Ce que fait le projet

| Domaine | Fonctions |
| --- | --- |
| **Supervision** | Mosaïque de 1 à 16 caméras, état en ligne / hors ligne, flux d'événements en direct, mode moniteur plein écran, caméras réparties sur plusieurs ordinateurs |
| **Détection et suivi** | Détection de personnes (YOLOv8) et suivi local (ByteTrack), ré-identification entre caméras |
| **Cibles** | Désigner une personne comme cible, la suivre sur plusieurs caméras, afficher son trajet sur le plan et le rejouer |
| **Alertes** | Alertes limitées aux cibles (option modifiable), priorités, regroupement des doublons, prise en charge par l'opérateur, notifications externes optionnelles |
| **Autorités** | Chaque caméra est rattachée à une autorité (ou à la plus proche sur la carte) qui reçoit les alertes de cible dans une **boîte de réception interne** : fiche connue de la cible, photo prise à l'instant de la détection, alarme sonore, prise en charge, suppression. Sans service externe (Telegram reste facultatif) |
| **Application Android** | Application Flutter (`appli/`) qui reçoit les alertes de l'autorité en arrière-plan, avec notification sonore et fiche détaillée de la cible |
| **Carte** | Carte d'Ouagadougou (ou plans par étage) avec les caméras placées, direction et champ de vision, trajet des cibles affiché sur la carte et rejoué dans l'historique |

<table>
<tr>
<td><img src="docs/screenshots/carte.png" alt="Carte et couverture des caméras"></td>
<td><img src="docs/screenshots/alertes.png" alt="Centre d'alertes"></td>
</tr>
</table>

## Pile technique

- **Backend** : Python 3.10+, FastAPI, Uvicorn (HTTP et WebSocket), SQLAlchemy 2 avec SQLite
- **Vision** : OpenCV, YOLOv8 et ByteTrack (Ultralytics), InsightFace (reconnaissance faciale, optionnelle)
- **Frontend** : HTML, CSS et JavaScript sans framework ni étape de build, avec thème clair / sombre et interface adaptée au mobile
- **Application mobile** : Flutter (Android), service d'arrière-plan et notifications locales
- **Tests** : `unittest`, plus des tests de la logique JavaScript exécutés avec Node.js

## Architecture

```
Caméras (webcam, RTSP, flux d'un autre PC)
        │
   Capture + détection + suivi (un fil par caméra)
        │
   ┌────┴────────────────────────────┐
   │ Cibles · Suivi inter-caméras    │──► Fil d'événements ──► Alertes · WebSocket
   └────┬────────────────────────────┘                              │
        │                                                           ▼
   SQLite (détections, alertes, plans…)             Interface web (FastAPI + pages statiques)
```

Chaque module reste indépendant du navigateur : le suivi des cibles, les comptages et les alertes tournent côté serveur, que la page soit ouverte ou non. Le code est dans `backend/app/` (un fichier par domaine : `alerts.py`, `floors.py`…), les pages dans `backend/app/static/`.

## Lancer avec de vraies caméras

```powershell
Copy-Item cameras.example.json cameras.json     # facultatif : import initial des caméras
.\venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Les caméras se gèrent ensuite depuis la page « Gestion des caméras ». L'application principale écoute uniquement sur `127.0.0.1`.

Pour que les téléphones des autorités puissent lire leur boîte de réception, lancer plutôt `lancer.bat` (ou `lancer.ps1`), ou cocher « Ouvrir la boîte de réception aux téléphones du réseau local » sur la page **Autorités**. Seule la boîte de réception (port 8090, liens secrets) est alors ouverte au réseau ; il faut aussi autoriser ce port dans le pare-feu Windows. Le guide complet (configuration, variables d'environnement, caméras distantes, dépannage) est dans [docs/DOCUMENTATION.md](docs/DOCUMENTATION.md).

## Application Android (alertes des autorités)

```powershell
cd appli
flutter pub get
flutter run          # téléphone Android branché, sur le même Wi-Fi que le serveur
```

À la première ouverture, coller le lien de l'autorité (page **Autorités**, bouton « Copier »). L'application garde le lien sur le téléphone, interroge la boîte de réception toutes les 3 secondes dans un service d'arrière-plan et notifie chaque nouvelle alerte (son et vibration). Elle affiche la liste des alertes, la fiche de la cible (photos, identité, confiance, derniers passages), la prise en charge et la suppression. Supprimer une alerte la retire de la boîte de l'autorité ; le journal du poste de commandement la conserve.

Sur Windows, si la compilation échoue avec « different roots » (projet sur `D:`, cache pub sur `C:`), `android/gradle.properties` désactive déjà la compilation incrémentale de Kotlin.

## Tests

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -t . -v
```

555 tests, sans caméra ni modèle : suivi des cibles, identité des personnes suivies, alertes et envoi aux autorités, boîte de réception, migrations de base de données, API, validation des entrées. Un test vérifie aussi qu'aucune page n'utilise `innerHTML`. L'application Android a ses propres tests (`cd appli; flutter test`) pour la lecture du lien et des alertes.

## Choix de conception

- **Vie privée par défaut** : les notifications externes sont anonymes ; les identités n'apparaissent que dans les écrans nominatifs (investigation, fiche de la cible) et dans la boîte de réception de l'autorité concernée. Les empreintes faciales ne sont jamais transmises, les photos d'alerte ne sont gardées qu'en mémoire et ce sont toujours des images prises par la caméra, jamais la photo de référence. Les secrets passent uniquement par des variables d'environnement.
- **Sécurité** : serveur local, plans importés assainis (SVG sans script), aucun `innerHTML`, validation stricte des entrées, adresses de flux jamais renvoyées en clair par l'API.
- **Migrations sans perte** : une ancienne base SQLite est mise à niveau au démarrage.

## Limites connues

- Prototype : la détection, le suivi et la reconnaissance n'ont **pas été évalués sur de vraies caméras** et les seuils ne sont pas calibrés.
- La couverture sur plan est géométrique : elle ignore murs et obstacles, c'est donc un maximum, et à l'échelle d'une ville elle n'a pas de sens physique.
- Le multi-sites n'est pas implémenté.
- Les notifications e-mail et Telegram ne sont testées qu'avec des simulations.
- La boîte de réception des autorités est en **HTTP non chiffré** : le lien secret est le seul contrôle d'accès. À utiliser sur un réseau isolé ou de confiance.
- L'application Android n'a pas été validée dans la durée : certains téléphones coupent les services d'arrière-plan malgré les réglages de batterie. Pas de version iPhone.
- Une identité maintenue sans visage visible (quelques secondes) peut être erronée si deux personnes se croisent.
- Testé sous Windows avec Chromium ; Firefox et Safari ne sont pas vérifiés.

## Données personnelles et usage responsable

Le projet contient des fonctions de reconnaissance faciale. Il ne doit être utilisé qu'avec le **consentement des personnes enrôlées** et dans le respect de la réglementation locale sur la vidéosurveillance et les données biométriques. Aucune base, photo ni empreinte n'est fournie avec le code (voir `.gitignore`).

## Usage et cadre légal

Les usages envisagés (recherche ciblée, personnes disparues, vérification d'identité…) supposent un cadre légal, un consentement quand il s'agit de personnes civiles et un contrôle humain avant toute action. Au Burkina Faso, la loi n° 001-2021/AN sur la protection des données personnelles s'applique (déclaration à la CIL) : le résumé que nous en avons n'est pas une source officielle et doit être vérifié avec un juriste.

## Pour aller plus loin

[Documentation technique complète](docs/DOCUMENTATION.md) · [Plan de développement en 14 modules](docs/NIIFYEEN_2_0_PLAN.md)
