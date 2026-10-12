# AI-ROOM-OBJ — DRONE TO AI

Convertit un scan de salle / de site (**OBJ**, par exemple issu d'une photogrammétrie par drone) en **DAE (Collada)**
directement importable dans l'importeur « SketchUp » d'**Adamson ArrayIntelligence** (AI), avec un aperçu 3D pour
orienter, placer et recadrer le modèle avant l'export.

> Projet indépendant, non affilié à Adamson. Ce dépôt ne contient aucun code ni fichier d'Adamson.

## Fonctions
- **Aperçu 3D fluide** du maillage (surface échantillonnée, textures / couleurs du `.mtl` si présentes).
  Glisser = tourner · clic droit + glisser (ou Maj + glisser) = déplacer · molette = zoom vers le curseur.
- **Placement** : rotation par pas de 90° autour des axes, réglage fin en degrés, mise à niveau automatique du sol,
  position X / Y / Z en mètres, centrage, sol à Z = 0.
- **Repère XYZ = origine d'AI** dessiné dans l'aperçu : on l'attrape à la souris (ou « origine = point cliqué ») pour
  poser l'origine d'AI exactement où l'on veut sur le modèle.
- **Recadrage** : boîte min / max, ou **polygone tracé en vue de dessus** ; la partie exclue est grisée.
- **Export DAE compatible AI** (voir ci-dessous), unités et axe vertical convertis, réduction optionnelle du nombre de triangles.

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

## Télécharger l'app (macOS)
Dans l'onglet **Releases** : `DRONE-TO-AI-V…-macOS-arm64.zip` (Mac Apple Silicon, macOS 11+). Décompresser, puis au premier
lancement faire **clic droit → Ouvrir** : l'app n'est pas notarisée par Apple. Si macOS la bloque quand même :
`xattr -dr com.apple.quarantine "DRONE TO AI.app"`.

## Construire l'app soi-même (macOS)
```bash
pip3 install -r requirements-build.txt
./build_app.sh                 # -> dist/DRONE TO AI.app
```
Le script lance l'autotest, construit l'app avec PyInstaller, relance l'autotest **depuis l'app construite** et la signe
« ad hoc ». Glisser un `.obj` sur l'icône (ou *Ouvrir avec…*) le charge directement.

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
| `build_app.sh`, `AI-ROOM-OBJ.spec`, `assets/` | Construction de l'app macOS et icône |
| `version.sh` | Calcul du numéro `V AA.MM.XX` |
