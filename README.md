# DRONE-TO-ADAMSON-AI — DRONE TO AI

Convertit un scan de salle / de site (**OBJ**, par exemple issu d'une photogrammétrie par drone) en **DAE (Collada)**
directement importable dans l'importeur « SketchUp » d'**Adamson ArrayIntelligence** (AI), avec un aperçu 3D pour
orienter, placer et recadrer le modèle avant l'export.

> Projet indépendant, non affilié à Adamson. Ce dépôt ne contient aucun code ni fichier d'Adamson.

## Fonctions
- **Aperçu 3D fluide** du maillage (surface échantillonnée, textures / couleurs du `.mtl` si présentes).
  Glisser = tourner · clic droit + glisser (ou Maj + glisser) = déplacer · molette = zoom vers le curseur.
- **Placement** : rotation **autour du repère XYZ** (le modèle tourne comme un solide autour de l'origine d'AI ;
  pour pivoter autour d'un point, on y pose d'abord l'origine). À l'écran **le modèle reste fixe et c'est le repère qui pivote** :
  boutons ±90° (animés) ou **anneaux** rouge / vert / bleu autour du repère à attraper et glisser (aimant tous les 15°), réglage fin en degrés, mise à niveau automatique du sol,
  position X / Y / Z en mètres, centrage, sol à Z = 0.
- **Repère XYZ = origine d'AI** dessiné dans l'aperçu, avec ses légendes (Z bleu = *Top*, Y vert = *Cours*, X rouge = *Nez*, modifiables dans `theme.py`) : on l'attrape à la souris (ou « origine = point cliqué ») pour
  poser l'origine d'AI exactement où l'on veut sur le modèle.
- **Recadrage** : boîte min / max, ou **polygone tracé en vue de dessus** ; la partie exclue est grisée.
- **Relevé topographique depuis internet** (au lieu d'un OBJ) : on **trace un polygone sur une carte satellite** (Esri, orthophoto IGN
  ou plan OpenStreetMap ; recherche d'adresse, zoom à la molette) ou on indique une adresse + une taille, et l'app télécharge le relief
  et construit le maillage 3D, découpé selon le polygone. Sources gratuites, sans clé : **IGN Géoplateforme** (France : terrain nu RGE ALTI
  ou surface avec bâtiments et arbres, pas de 1 m), **AWS Terrain Tiles** (monde, 3 à 30 m) ; adresses via OpenStreetMap Nominatim.
  L'aperçu 3D est texturé avec l'image satellite ; le DAE exporté reste sans texture (AI les ignore). Ligne de commande :
  `python3 topo.py "adresse" sortie.dae --size 600`.
- **Export DAE compatible AI** (voir ci-dessous), unités et axe vertical convertis, réduction du nombre de triangles (activée par défaut, cible 1 000).

## Lancer depuis le code source
```bash
pip3 install -r requirements.txt
python3 app.py                 # ou : python3 app.py modele.obj
```
Python 3.10+ avec Tk (le Python de python.org pour macOS convient). Ligne de commande sans interface :
```bash
python3 obj2dae.py modele.obj sortie.dae --src-up Z --auto-level --rot 0 0 90 --center --floor
python3 obj2dae.py modele.obj sortie.dae --polygon -40 -40  40 -40  40 0  0 0  0 40  -40 40
python3 app.py --selftest      # vérifie le moteur de conversion, sans fenêtre
```

## Télécharger l'app
### macOS
Dans l'onglet **Releases** : `DRONE-TO-AI-V…-macOS-arm64.zip` (Mac Apple Silicon, macOS 11+). Décompresser, puis au premier
lancement faire **clic droit → Ouvrir** : l'app n'est pas notarisée par Apple. Si macOS la bloque quand même :
`xattr -dr com.apple.quarantine "DRONE TO AI.app"`.

### Windows
Dans **Releases** : `DRONE-TO-AI-V…-windows-x64.zip`. Décompresser le dossier, puis lancer `DRONE TO AI.exe`. L'exécutable n'est pas signé :
Windows SmartScreen peut afficher « Windows a protégé votre ordinateur » → **Informations complémentaires → Exécuter quand même**.
Glisser un `.obj` sur l'exécutable le charge directement.
La version Windows est construite automatiquement par GitHub Actions (`.github/workflows/windows.yml`) à chaque release ;
le build lance un test de l'interface sur un vrai serveur Windows et conserve une capture d'écran en artefact.

## Construire l'app soi-même
**macOS**
```bash
pip3 install -r requirements-build.txt
./build_app.sh                 # -> dist/DRONE TO AI.app
```
Le script lance l'autotest, construit l'app avec PyInstaller, relance l'autotest **depuis l'app construite** et la signe
« ad hoc ». Glisser un `.obj` sur l'icône (ou *Ouvrir avec…*) le charge directement.

**Windows** (depuis Git Bash, avec Python de python.org)
```bash
pip install -r requirements-build.txt
./build_windows.sh             # -> dist/DRONE-TO-AI-V…-windows-x64.zip
```

## Mises à jour
L'app vérifie au démarrage (en arrière-plan, sans jamais bloquer l'interface) si une version plus récente est publiée dans les
**Releases** du dépôt. Si oui, un bouton « Mise à jour V… » apparaît en haut à droite et ouvre le téléchargement adapté à ton système.
Onglet MODÈLE → **Version** : « Rechercher une mise à jour » (à la demande) et case « Vérifier au démarrage » (désactivable ;
préférence enregistrée dans le dossier de configuration de l'utilisateur).
La vérification interroge uniquement l'API publique de GitHub (`api.github.com`), sans compte ni jeton, et n'envoie rien d'autre que la
requête HTTPS. Lancée depuis le code source (version « dev »), elle ne s'exécute pas au démarrage ; `python3 app.py --check-update` la fait à la main.

## Numéro de version
Format **`V AA.MM.XX`** : `AA` année (2 chiffres), `MM` mois, `XX` nombre de commits faits durant le mois (remis à zéro
chaque début de mois). Exemple : `V26.10.02` = 2ᵉ commit d'octobre 2026. Calculé par `version.sh` à partir de
l'historique git, il apparaît dans le titre de la fenêtre et dans la version du bundle `.app`.
Pour publier : **commiter, construire (`build_app.sh`), puis taguer ce même commit** (`git tag "$(./version.sh)"`).

## Ce qu'AI attend d'un DAE
Contraintes constatées à l'usage et validées par un import réel dans AI :
- **un seul `<triangles>`** : AI écrase les indices à chaque groupe rencontré et ne garde que le dernier ;
- **une seule entrée `VERTEX`** par `<triangles>` (AI lit un indice par balise `<input>`), `POSITION` en premier dans `<vertices>` ;
- les matériaux et textures sont ignorés ; un maillage énorme peut saturer la mémoire d'AI (réduire le nombre de triangles).

L'app écrit exactement cette structure (option « Un seul groupe de triangles », activée par défaut).

## Fichiers
| Fichier | Rôle |
|---|---|
| `app.py` | Interface (onglets MODÈLE / PLACEMENT / RECADRAGE / EXPORT) |
| `preview3d.py` | Vue 3D : rendu numpy, repère XYZ, polygone de recadrage |
| `obj2dae.py` | Moteur : lecture OBJ / MTL, rotation, recadrage, réduction, écriture DAE |
| `theme.py` | Thème visuel inspiré d'ArrayIntelligence |
| `updater.py`, `settings.py` | Recherche de mise à jour (API GitHub) et préférences |
| `build_app.sh`, `DRONE-TO-ADAMSON-AI.spec`, `assets/` | Construction de l'app macOS et icône |
| `version.sh` | Calcul du numéro `V AA.MM.XX` |
