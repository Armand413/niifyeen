import 'package:flutter_test/flutter_test.dart';

import 'package:appli/details.dart';

const known = '''ALERTE — faisal détectée
Lieu : Entrée (Hall principal)
Heure : 14:03:22
Confiance du système : 87% (estimation, à vérifier sur place)
Vue juste avant : Salle

Personne reconnue dans la base :
  Nom : Faisal K.
  Référence : MAT-042
  Informations : Étudiant, 3e année.
Porte un sac bleu.
  Fiche active : oui
  Correspondance : 91% (peut être fausse)

Derniers passages :
  14:01:10 — Salle
  14:03:22 — Entrée

Photos jointes : gros plan de la cible et image de la caméra à cet instant (cible encadrée).
Message automatique NiifYéén (prototype). Vérifier l'identité avant toute action.''';

void main() {
  test('alerte avec personne reconnue', () {
    final d = AlertDetails.parse(known);
    expect(d.place, 'Entrée (Hall principal)');
    expect(d.time, '14:03:22');
    expect(d.confidence, closeTo(0.87, 0.001));
    expect(d.previous, 'Salle');
    expect(d.known, true);
    expect(d.name, 'Faisal K.');
    expect(d.reference, 'MAT-042');
    expect(d.infos, 'Étudiant, 3e année.\nPorte un sac bleu.');
    expect(d.active, true);
    expect(d.match, closeTo(0.91, 0.001));
    expect(d.path.length, 2);
    expect(d.path.last.camera, 'Entrée');
    expect(d.path.first.time, '14:01:10');
  });

  test('personne inconnue', () {
    final d = AlertDetails.parse('ALERTE — X détectée\nLieu : Sortie\nHeure : 10:00:00\n\n'
        'Personne non enregistrée dans la base : identité inconnue du système.\n');
    expect(d.known, false);
    expect(d.name, '');
    expect(d.place, 'Sortie');
  });

  test('texte non reconnu ne plante pas', () {
    final d = AlertDetails.parse('n’importe quoi');
    expect(d.known, isNull);
    expect(d.path, isEmpty);
    expect(d.raw, 'n’importe quoi');
  });
}
