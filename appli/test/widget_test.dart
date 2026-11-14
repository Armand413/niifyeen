import 'package:flutter_test/flutter_test.dart';

import 'package:appli/api.dart';

void main() {
  test('lien complet valide', () {
    final l = InboxLink.parse('http://192.168.1.20:8090/a/abc123');
    expect(l, isNotNull);
    expect(l!.token, 'abc123');
    expect(l.api('messages').toString(), 'http://192.168.1.20:8090/a/abc123/api/messages');
    expect(l.stored, 'http://192.168.1.20:8090/a/abc123');
  });

  test('lien avec barre finale et espaces', () {
    final l = InboxLink.parse('  http://10.0.0.5:8090/a/xyz/ ');
    expect(l?.token, 'xyz');
  });

  test('liens invalides', () {
    expect(InboxLink.parse(''), isNull);
    expect(InboxLink.parse('192.168.1.20:8090/a/abc'), isNull);
    expect(InboxLink.parse('http://192.168.1.20:8090/'), isNull);
    expect(InboxLink.parse('ftp://192.168.1.20/a/abc'), isNull);
  });

  test('alerte depuis le JSON du serveur', () {
    final a = Alert.fromJson({
      'id': 7,
      'created_at': '2026-11-12T14:00:00Z',
      'target_label': 'faisal',
      'camera_label': 'Entrée',
      'camera_place': 'Hall',
      'message': 'Cible : faisal',
      'acknowledged': false,
      'photos': ['Gros plan de la cible', 'Image de la caméra'],
    });
    expect(a.id, 7);
    expect(a.photos.length, 2);
    expect(a.acknowledged, false);
  });
}
