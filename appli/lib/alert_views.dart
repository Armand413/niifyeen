import 'package:flutter/material.dart';

import 'api.dart';
import 'details.dart';

const navy = Color(0xFF101B2D);
const amber = Color(0xFFE8B23C);
const paper = Color(0xFFF6F3EC);

String ago(DateTime? t) {
  if (t == null) return '';
  final d = DateTime.now().difference(t);
  if (d.inSeconds < 45) return 'à l’instant';
  if (d.inMinutes < 60) return 'il y a ${d.inMinutes} min';
  if (d.inHours < 24) return 'il y a ${d.inHours} h';
  return formatTime(t);
}

/// Couleur et mot pour la confiance du système (estimation, jamais une preuve).
({Color color, String word}) confidenceLook(double v) {
  if (v >= 0.8) return (color: const Color(0xFF1E7A52), word: 'élevée');
  if (v >= 0.6) return (color: const Color(0xFFB87A00), word: 'moyenne');
  return (color: const Color(0xFFB3402A), word: 'faible');
}

class _Chip extends StatelessWidget {
  final String text;
  final Color bg;
  final Color fg;
  final IconData? icon;
  const _Chip(this.text, this.bg, this.fg, {this.icon});
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(20)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        if (icon != null) ...[Icon(icon, size: 14, color: fg), const SizedBox(width: 4)],
        Text(text, style: TextStyle(fontSize: 12, fontWeight: FontWeight.w600, color: fg)),
      ]),
    );
  }
}

Widget _identityChip(AlertDetails d) {
  if (d.known == true) {
    return const _Chip('Personne reconnue', Color(0xFFD5EBDD), Color(0xFF154A35), icon: Icons.verified_user);
  }
  if (d.known == false) {
    return const _Chip('Identité inconnue', Color(0xFFF0D4CC), Color(0xFF7A2E1C), icon: Icons.help_outline);
  }
  return const SizedBox.shrink();
}

/// Carte de la liste : coup d'œil rapide (photo, cible, lieu, heure, confiance).
class AlertCard extends StatelessWidget {
  final Alert alert;
  final InboxApi api;
  final VoidCallback onChanged;
  const AlertCard({super.key, required this.alert, required this.api, required this.onChanged});

  @override
  Widget build(BuildContext context) {
    final d = AlertDetails.parse(alert.message);
    final where = [alert.camera, alert.place].where((s) => s.isNotEmpty).join(' · ');
    final pending = !alert.acknowledged;
    return Card(
      elevation: 0,
      color: Colors.white,
      margin: const EdgeInsets.only(top: 10),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: BorderSide(color: pending ? amber : const Color(0xFFDDD6C6), width: pending ? 1.6 : 1),
      ),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: () async {
          final changed = await Navigator.push<bool>(
            context,
            MaterialPageRoute(builder: (_) => AlertDetailPage(alert: alert, api: api)),
          );
          if (changed == true) onChanged();
        },
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(12),
              child: alert.photos.isEmpty
                  ? _placeholder(84)
                  : Image.network(api.photoUrl(alert.id, 0), width: 84, height: 84, fit: BoxFit.cover,
                      errorBuilder: (_, __, ___) => _placeholder(84)),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(alert.target.isEmpty ? 'Cible inconnue' : alert.target,
                    maxLines: 1, overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700, color: navy)),
                const SizedBox(height: 2),
                Text(where, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: Colors.black87)),
                Text(ago(alert.createdAt), style: const TextStyle(color: Colors.black54, fontSize: 13)),
                const SizedBox(height: 6),
                Wrap(spacing: 6, runSpacing: 4, children: [
                  _identityChip(d),
                  if (d.confidence != null)
                    _Chip('Confiance ${(d.confidence! * 100).round()} %', const Color(0xFFEFEBE0), confidenceLook(d.confidence!).color),
                  if (!pending) const _Chip('Pris en charge', Color(0xFFD5EBDD), Color(0xFF154A35), icon: Icons.check),
                ]),
              ]),
            ),
            const Icon(Icons.chevron_right, color: Colors.black38),
          ]),
        ),
      ),
    );
  }

  static Widget _placeholder(double s) => Container(
      width: s, height: s, color: const Color(0xFFEFEBE0), child: const Icon(Icons.person, color: Colors.black38, size: 36));
}

/// Fiche complète d'une alerte : photos, identité, détection, parcours, bouton de prise en charge.
class AlertDetailPage extends StatefulWidget {
  final Alert alert;
  final InboxApi api;
  const AlertDetailPage({super.key, required this.alert, required this.api});
  @override
  State<AlertDetailPage> createState() => _AlertDetailPageState();
}

class _AlertDetailPageState extends State<AlertDetailPage> {
  bool _busy = false;
  late bool _done = widget.alert.acknowledged;

  Future<void> _ack() async {
    setState(() => _busy = true);
    bool ok = false;
    try {
      ok = await widget.api.acknowledge(widget.alert.id);
    } catch (_) {}
    if (!mounted) return;
    if (ok) {
      Navigator.pop(context, true);
    } else {
      setState(() => _busy = false);
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Échec : serveur injoignable')));
    }
  }

  Future<void> _delete() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Supprimer cette alerte ?'),
        content: const Text('Elle disparaît de votre boîte de réception. Le poste de commandement en garde la trace dans son journal.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Annuler')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Supprimer')),
        ],
      ),
    );
    if (ok != true) return;
    bool done = false;
    try {
      done = await widget.api.deleteAlert(widget.alert.id);
    } catch (_) {}
    if (!mounted) return;
    if (done) {
      Navigator.pop(context, true);
    } else {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Échec : serveur injoignable')));
    }
  }

  Widget _photo(int i) {
    final a = widget.alert;
    final url = widget.api.photoUrl(a.id, i);
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        GestureDetector(
          onTap: () => Navigator.push(
            context,
            MaterialPageRoute(
              builder: (_) => Scaffold(
                backgroundColor: Colors.black,
                appBar: AppBar(title: Text(a.photos[i])),
                body: Center(child: InteractiveViewer(child: Image.network(url))),
              ),
            ),
          ),
          child: ClipRRect(
            borderRadius: BorderRadius.circular(14),
            child: Container(
              color: Colors.black,
              width: double.infinity,
              constraints: BoxConstraints(maxHeight: i == 0 ? 320 : 220),
              child: Image.network(url, fit: BoxFit.contain,
                  errorBuilder: (_, __, ___) => const SizedBox(
                      height: 120, child: Center(child: Text('Photo expirée (gardée en mémoire seulement)', style: TextStyle(color: Colors.white70))))),
            ),
          ),
        ),
        const SizedBox(height: 4),
        Text(a.photos[i], style: const TextStyle(fontSize: 12, color: Colors.black54)),
      ]),
    );
  }

  @override
  Widget build(BuildContext context) {
    final a = widget.alert;
    final d = AlertDetails.parse(a.message);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Fiche de la cible'),
        actions: [IconButton(tooltip: 'Supprimer', icon: const Icon(Icons.delete_outline), onPressed: _delete)],
      ),
      bottomNavigationBar: _done
          ? null
          : SafeArea(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
                child: FilledButton.icon(
                  style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(52), backgroundColor: navy),
                  onPressed: _busy ? null : _ack,
                  icon: const Icon(Icons.check),
                  label: Text(_busy ? 'Envoi…' : 'Pris en charge'),
                ),
              ),
            ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text(a.target.isEmpty ? 'Cible inconnue' : a.target,
              style: const TextStyle(fontSize: 26, fontWeight: FontWeight.w800, color: navy)),
          const SizedBox(height: 6),
          Wrap(spacing: 8, runSpacing: 6, children: [
            _identityChip(d),
            _Chip(ago(a.createdAt), const Color(0xFFEFEBE0), Colors.black87, icon: Icons.schedule),
            if (_done) const _Chip('Pris en charge', Color(0xFFD5EBDD), Color(0xFF154A35), icon: Icons.check),
          ]),
          const SizedBox(height: 14),
          for (var i = 0; i < a.photos.length; i++) _photo(i),

          _Section('Identité', Icons.badge_outlined, [
            if (d.known == true) ...[
              _Row('Nom', d.name),
              if (d.reference.isNotEmpty) _Row('Référence', d.reference),
              if (d.active != null) _Row('Fiche', d.active! ? 'Active' : 'Inactive'),
              if (d.infos.isNotEmpty) _Row('Informations', d.infos),
              if (d.match != null) _Meter('Correspondance du visage', d.match!, note: 'Peut être fausse : vérifier sur place.'),
            ] else if (d.known == false)
              const Text('Personne non enregistrée dans la base : identité inconnue du système.')
            else
              const Text('Aucune information d’identité dans cette alerte.'),
          ]),

          _Section('Détection', Icons.videocam_outlined, [
            if (a.camera.isNotEmpty) _Row('Caméra', a.camera),
            if (a.place.isNotEmpty) _Row('Emplacement', a.place),
            if (d.time.isNotEmpty) _Row('Heure', d.time),
            if (d.previous.isNotEmpty) _Row('Vue juste avant', d.previous),
            if (d.confidence != null) _Meter('Confiance du système', d.confidence!, note: 'Estimation, à vérifier sur place.'),
          ]),

          if (d.path.isNotEmpty)
            _Section('Derniers passages', Icons.route, [
              for (var i = 0; i < d.path.length; i++) _PathRow(d.path[i], first: i == 0, last: i == d.path.length - 1),
            ]),

          Container(
            margin: const EdgeInsets.only(top: 4, bottom: 12),
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(color: const Color(0xFFF6E3B4), borderRadius: BorderRadius.circular(12)),
            child: const Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Icon(Icons.info_outline, size: 18, color: Color(0xFF6B4500)),
              SizedBox(width: 8),
              Expanded(
                child: Text('Message automatique NiifYéén (prototype). Vérifier l’identité avant toute action.',
                    style: TextStyle(fontSize: 13, color: Color(0xFF6B4500))),
              ),
            ]),
          ),
          if (d.known == null && d.place.isEmpty)
            SelectableText(d.raw, style: const TextStyle(fontSize: 13)), // texte non reconnu : on l'affiche tel quel
        ],
      ),
    );
  }
}

class _Section extends StatelessWidget {
  final String title;
  final IconData icon;
  final List<Widget> children;
  const _Section(this.title, this.icon, this.children);
  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: const Color(0xFFDDD6C6)),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Icon(icon, size: 20, color: const Color(0xFF8A5A00)),
          const SizedBox(width: 8),
          Text(title, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700, color: navy)),
        ]),
        const SizedBox(height: 10),
        ...children,
      ]),
    );
  }
}

class _Row extends StatelessWidget {
  final String label;
  final String value;
  const _Row(this.label, this.value);
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SizedBox(width: 112, child: Text(label, style: const TextStyle(color: Colors.black54, fontSize: 13))),
        Expanded(child: SelectableText(value.isEmpty ? '—' : value, style: const TextStyle(fontSize: 15))),
      ]),
    );
  }
}

class _Meter extends StatelessWidget {
  final String label;
  final double value;
  final String note;
  const _Meter(this.label, this.value, {required this.note});
  @override
  Widget build(BuildContext context) {
    final look = confidenceLook(value);
    return Padding(
      padding: const EdgeInsets.only(top: 4, bottom: 4),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Expanded(child: Text(label, style: const TextStyle(color: Colors.black54, fontSize: 13))),
          Text('${(value * 100).round()} % · ${look.word}',
              style: TextStyle(fontWeight: FontWeight.w700, color: look.color)),
        ]),
        const SizedBox(height: 6),
        ClipRRect(
          borderRadius: BorderRadius.circular(6),
          child: LinearProgressIndicator(
            value: value, minHeight: 8, color: look.color, backgroundColor: const Color(0xFFEFEBE0)),
        ),
        const SizedBox(height: 4),
        Text(note, style: const TextStyle(fontSize: 12, color: Colors.black54)),
      ]),
    );
  }
}

class _PathRow extends StatelessWidget {
  final PathStep step;
  final bool first;
  final bool last;
  const _PathRow(this.step, {required this.first, required this.last});
  @override
  Widget build(BuildContext context) {
    return IntrinsicHeight(
      child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SizedBox(
          width: 24,
          child: Column(children: [
            Expanded(child: Container(width: 2, color: first ? Colors.transparent : const Color(0xFFDDD6C6))),
            Container(
              width: 12, height: 12,
              decoration: BoxDecoration(color: last ? amber : const Color(0xFFB8C2D0), shape: BoxShape.circle),
            ),
            Expanded(child: Container(width: 2, color: last ? Colors.transparent : const Color(0xFFDDD6C6))),
          ]),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Row(children: [
              Text(step.time, style: const TextStyle(color: Colors.black54, fontSize: 13)),
              const SizedBox(width: 10),
              Expanded(child: Text(step.camera, style: TextStyle(fontWeight: last ? FontWeight.w700 : FontWeight.w400))),
            ]),
          ),
        ),
      ]),
    );
  }
}
