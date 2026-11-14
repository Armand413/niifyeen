import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

/// Lien de la boîte de réception d'une autorité : http://ADRESSE:8090/a/JETON
class InboxLink {
  final Uri base;
  final String token;
  const InboxLink(this.base, this.token);

  /// Accepte le lien complet copié depuis la page « Autorités » de NiifYéén.
  static InboxLink? parse(String raw) {
    final uri = Uri.tryParse(raw.trim());
    if (uri == null || !uri.hasAuthority || uri.host.isEmpty) return null;
    if (uri.scheme != 'http' && uri.scheme != 'https') return null;
    final segs = uri.pathSegments.where((s) => s.isNotEmpty).toList();
    final i = segs.indexOf('a');
    if (i < 0 || i + 1 >= segs.length) return null;
    final base = Uri(scheme: uri.scheme, host: uri.host, port: uri.hasPort ? uri.port : null);
    return InboxLink(base, segs[i + 1]);
  }

  Uri api(String path) => base.replace(path: '/a/$token/api/$path');
  String get stored => base.replace(path: '/a/$token').toString();
}

class Alert {
  final int id;
  final DateTime? createdAt;
  final String target;
  final String camera;
  final String place;
  final String message;
  final bool acknowledged;
  final List<String> photos;

  const Alert({
    required this.id,
    required this.createdAt,
    required this.target,
    required this.camera,
    required this.place,
    required this.message,
    required this.acknowledged,
    required this.photos,
  });

  factory Alert.fromJson(Map<String, dynamic> j) => Alert(
        id: (j['id'] as num).toInt(),
        createdAt: DateTime.tryParse((j['created_at'] ?? '').toString())?.toLocal(),
        target: (j['target_label'] ?? '').toString(),
        camera: (j['camera_label'] ?? j['camera_id'] ?? '').toString(),
        place: (j['camera_place'] ?? '').toString(),
        message: (j['message'] ?? '').toString(),
        acknowledged: j['acknowledged'] == true,
        photos: ((j['photos'] as List?) ?? const []).map((e) => e.toString()).toList(),
      );
}

class InboxData {
  final String authority;
  final String service;
  final List<Alert> alerts;
  const InboxData(this.authority, this.service, this.alerts);
}

class InboxApi {
  final InboxLink link;
  const InboxApi(this.link);

  Future<InboxData> fetch() async {
    final res = await http.get(link.api('messages')).timeout(const Duration(seconds: 5));
    if (res.statusCode != 200) throw Exception('HTTP ${res.statusCode}');
    final j = jsonDecode(utf8.decode(res.bodyBytes)) as Map<String, dynamic>;
    final alerts = ((j['messages'] as List?) ?? const [])
        .map((e) => Alert.fromJson(e as Map<String, dynamic>))
        .toList();
    return InboxData((j['authority'] ?? '').toString(), (j['service'] ?? '').toString(), alerts);
  }

  Future<bool> deleteAlert(int id) async {
    final res = await http.delete(link.api('messages/$id')).timeout(const Duration(seconds: 5));
    return res.statusCode == 200;
  }

  Future<bool> deleteAll() async {
    final res = await http.delete(link.api('messages')).timeout(const Duration(seconds: 5));
    return res.statusCode == 200;
  }

  String photoUrl(int id, int index) => link.api('photo/$id/$index').toString();

  Future<bool> acknowledge(int id) async {
    final res = await http.post(link.api('ack/$id')).timeout(const Duration(seconds: 5));
    return res.statusCode == 200;
  }
}

/// Stockage local (le lien est un secret : il ne quitte pas le téléphone).
class Store {
  static const _linkKey = 'inbox_link';
  static const _lastKey = 'last_notified_id';

  static Future<InboxLink?> loadLink() async {
    final p = await SharedPreferences.getInstance();
    await p.reload();
    final raw = p.getString(_linkKey);
    return raw == null ? null : InboxLink.parse(raw);
  }

  static Future<void> saveLink(InboxLink link) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_linkKey, link.stored);
    await p.remove(_lastKey); // nouveau lien : on repart de l'état actuel, sans notifier l'ancien
  }

  static Future<void> clearLink() async {
    final p = await SharedPreferences.getInstance();
    await p.remove(_linkKey);
    await p.remove(_lastKey);
  }

  static Future<int?> loadLastNotified() async {
    final p = await SharedPreferences.getInstance();
    await p.reload();
    return p.getInt(_lastKey);
  }

  static Future<void> saveLastNotified(int id) async {
    final p = await SharedPreferences.getInstance();
    await p.setInt(_lastKey, id);
  }
}

String formatTime(DateTime? t) {
  if (t == null) return '';
  String two(int n) => n.toString().padLeft(2, '0');
  return '${two(t.hour)}:${two(t.minute)}:${two(t.second)} · ${two(t.day)}/${two(t.month)}';
}
