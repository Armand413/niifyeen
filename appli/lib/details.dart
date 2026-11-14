/// Lecture du texte d'alerte généré par NiifYéén (dispatch.compose_message) pour l'afficher en sections.
/// Tout ce qui n'est pas reconnu reste disponible dans `raw`.
class PathStep {
  final String time;
  final String camera;
  const PathStep(this.time, this.camera);
}

class AlertDetails {
  final String place;
  final String time;
  final double? confidence; // 0..1, estimation du système
  final String previous; // caméra qui voyait la cible juste avant
  final bool? known; // personne reconnue dans la base ? null = information absente
  final String name;
  final String reference;
  final String infos;
  final bool? active;
  final double? match; // 0..1
  final List<PathStep> path;
  final String raw;

  const AlertDetails({
    this.place = '',
    this.time = '',
    this.confidence,
    this.previous = '',
    this.known,
    this.name = '',
    this.reference = '',
    this.infos = '',
    this.active,
    this.match,
    this.path = const [],
    this.raw = '',
  });

  static String _val(String line) {
    final i = line.indexOf(' : ');
    return i < 0 ? '' : line.substring(i + 3).trim();
  }

  static double? _pct(String s) {
    final m = RegExp(r'(\d+(?:[.,]\d+)?)\s*%').firstMatch(s);
    if (m == null) return null;
    final v = double.tryParse(m.group(1)!.replaceAll(',', '.'));
    return v == null ? null : (v / 100).clamp(0.0, 1.0);
  }

  factory AlertDetails.parse(String message) {
    String place = '', time = '', previous = '', name = '', reference = '';
    final infos = StringBuffer();
    double? confidence, match;
    bool? known, active;
    final path = <PathStep>[];
    var section = '';

    for (final rawLine in message.split('\n')) {
      final line = rawLine.trim();
      if (line.isEmpty) continue;
      if (line.startsWith('Lieu :')) {
        place = _val(line);
      } else if (line.startsWith('Heure :')) {
        time = _val(line);
      } else if (line.startsWith('Confiance du système')) {
        confidence = _pct(line);
      } else if (line.startsWith('Vue juste avant')) {
        previous = _val(line);
      } else if (line.startsWith('Personne reconnue')) {
        known = true;
        section = 'person';
      } else if (line.startsWith('Personne non enregistrée')) {
        known = false;
        section = '';
      } else if (line.startsWith('Derniers passages')) {
        section = 'path';
      } else if (line.startsWith('Photos jointes') || line.startsWith('Message automatique')) {
        section = '';
      } else if (section == 'person') {
        if (line.startsWith('Nom :')) {
          name = _val(line);
        } else if (line.startsWith('Référence :')) {
          reference = _val(line);
        } else if (line.startsWith('Informations :')) {
          infos.write(_val(line));
        } else if (line.startsWith('Fiche active')) {
          active = _val(line).toLowerCase().startsWith('o');
        } else if (line.startsWith('Correspondance')) {
          match = _pct(line);
        } else if (infos.isNotEmpty) {
          infos.write('\n$line'); // suite d'un champ « Informations » sur plusieurs lignes
        }
      } else if (section == 'path') {
        final parts = line.split(' — ');
        if (parts.length >= 2) path.add(PathStep(parts.first.trim(), parts.sublist(1).join(' — ').trim()));
      }
    }
    return AlertDetails(
      place: place,
      time: time,
      confidence: confidence,
      previous: previous,
      known: known,
      name: name,
      reference: reference,
      infos: infos.toString(),
      active: active,
      match: match,
      path: path,
      raw: message,
    );
  }
}
