import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import 'api.dart';
import 'alert_views.dart';
import 'background.dart';

const _navy = Color(0xFF101B2D);
const _amber = Color(0xFFE8B23C);

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  FlutterForegroundTask.initCommunicationPort();
  runApp(const NiifyeenApp());
}

class NiifyeenApp extends StatelessWidget {
  const NiifyeenApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'NiifYéén Alertes',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: _navy, secondary: _amber),
        useMaterial3: true,
        scaffoldBackgroundColor: paper,
        appBarTheme: const AppBarTheme(backgroundColor: navy, foregroundColor: Colors.white),
      ),
      home: const Gate(),
    );
  }
}

/// Choisit l'écran : configuration si aucun lien n'est enregistré, sinon la liste des alertes.
class Gate extends StatefulWidget {
  const Gate({super.key});
  @override
  State<Gate> createState() => _GateState();
}

class _GateState extends State<Gate> {
  InboxLink? _link;
  bool _loaded = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final l = await Store.loadLink();
    if (!mounted) return;
    setState(() {
      _link = l;
      _loaded = true;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (!_loaded) return const Scaffold(body: Center(child: CircularProgressIndicator()));
    if (_link == null) return SetupPage(onSaved: _load);
    return AlertsPage(link: _link!, onDisconnect: () async {
      await stopBackgroundService();
      await Store.clearLink();
      await _load();
    });
  }
}

class SetupPage extends StatefulWidget {
  final Future<void> Function() onSaved;
  const SetupPage({super.key, required this.onSaved});
  @override
  State<SetupPage> createState() => _SetupPageState();
}

class _SetupPageState extends State<SetupPage> {
  final _ctrl = TextEditingController();
  String? _error;
  bool _busy = false;

  Future<void> _connect() async {
    final link = InboxLink.parse(_ctrl.text);
    if (link == null) {
      setState(() => _error = 'Lien invalide. Exemple : http://192.168.1.20:8090/a/votre-jeton');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await InboxApi(link).fetch();
    } catch (_) {
      setState(() {
        _busy = false;
        _error = 'Impossible de joindre le serveur. Vérifiez le Wi-Fi (même réseau que NiifYéén), l’adresse et le jeton.';
      });
      return;
    }
    await Store.saveLink(link);
    await widget.onSaved();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('NiifYéén Alertes')),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          const Text('Connexion à votre boîte de réception',
              style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600)),
          const SizedBox(height: 8),
          const Text('Collez le lien de votre autorité (page « Autorités » de NiifYéén, bouton Copier). '
              'Le téléphone doit être sur le même réseau Wi-Fi que le serveur.'),
          const SizedBox(height: 16),
          TextField(
            controller: _ctrl,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: InputDecoration(
              border: const OutlineInputBorder(),
              labelText: 'Lien de la boîte de réception',
              errorText: _error,
              errorMaxLines: 4,
            ),
          ),
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _busy ? null : _connect,
            child: Text(_busy ? 'Connexion…' : 'Se connecter'),
          ),
          const SizedBox(height: 24),
          const Text('Le lien est un secret : ne le partagez pas. Il reste uniquement sur ce téléphone. '
              'La liaison est en HTTP non chiffré sur le réseau local.',
              style: TextStyle(color: Colors.black54)),
        ],
      ),
    );
  }
}

class AlertsPage extends StatefulWidget {
  final InboxLink link;
  final Future<void> Function() onDisconnect;
  const AlertsPage({super.key, required this.link, required this.onDisconnect});
  @override
  State<AlertsPage> createState() => _AlertsPageState();
}

class _AlertsPageState extends State<AlertsPage> with WidgetsBindingObserver {
  late final InboxApi _api = InboxApi(widget.link);
  Timer? _timer;
  InboxData? _data;
  bool _online = false;
  bool _showAll = false;
  bool _needsBattery = false;
  bool _needsNotif = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _refresh();
    _timer = Timer.periodic(const Duration(seconds: 3), (_) => _refresh());
    WidgetsBinding.instance.addPostFrameCallback((_) => _startService());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _checkPermissions();
      _refresh();
    }
  }

  Future<void> _startService() async {
    final plugin = FlutterLocalNotificationsPlugin();
    await initNotifications(plugin);
    await plugin
        .resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>()
        ?.requestNotificationsPermission();
    await startBackgroundService();
    await _checkPermissions();
  }

  Future<void> _checkPermissions() async {
    final notif = await FlutterForegroundTask.checkNotificationPermission();
    final battery = await FlutterForegroundTask.isIgnoringBatteryOptimizations;
    if (!mounted) return;
    setState(() {
      _needsNotif = notif != NotificationPermission.granted;
      _needsBattery = !battery;
    });
  }

  Future<void> _refresh() async {
    try {
      final d = await _api.fetch();
      if (!mounted) return;
      setState(() {
        _data = d;
        _online = true;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() => _online = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final all = _data?.alerts ?? const <Alert>[];
    final todo = all.where((a) => !a.acknowledged).toList();
    final shown = _showAll ? all : todo;
    return Scaffold(
      appBar: AppBar(
        title: Text(_data == null || _data!.authority.isEmpty ? 'NiifYéén Alertes' : _data!.authority),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 4),
            child: Center(
              child: Text(_online ? '● en ligne' : '● hors ligne',
                  style: TextStyle(color: _online ? Colors.greenAccent.shade200 : Colors.redAccent.shade100, fontSize: 13)),
            ),
          ),
          IconButton(
            tooltip: 'Tout supprimer',
            icon: const Icon(Icons.delete_sweep_outlined),
            onPressed: all.isEmpty
                ? null
                : () async {
                    final ok = await showDialog<bool>(
                      context: context,
                      builder: (c) => AlertDialog(
                        title: const Text('Supprimer toutes les alertes ?'),
                        content: const Text('Elles disparaissent de votre boîte de réception. Le poste de commandement en garde la trace dans son journal.'),
                        actions: [
                          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Annuler')),
                          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Tout supprimer')),
                        ],
                      ),
                    );
                    if (ok == true) {
                      try {
                        await _api.deleteAll();
                      } catch (_) {}
                      await _refresh();
                    }
                  },
          ),
          IconButton(
            tooltip: 'Changer de lien',
            icon: const Icon(Icons.link_off),
            onPressed: () async {
              final ok = await showDialog<bool>(
                context: context,
                builder: (c) => AlertDialog(
                  title: const Text('Se déconnecter ?'),
                  content: const Text('Les alertes ne seront plus reçues sur ce téléphone.'),
                  actions: [
                    TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Annuler')),
                    FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Déconnecter')),
                  ],
                ),
              );
              if (ok == true) await widget.onDisconnect();
            },
          ),
        ],
      ),
      body: Column(
        children: [
          if (_needsNotif)
            _Banner('Les notifications sont bloquées : activez-les pour entendre les alertes.',
                'Autoriser', () async {
              await FlutterForegroundTask.requestNotificationPermission();
              await _checkPermissions();
            }),
          if (_needsBattery)
            _Banner('Pour recevoir les alertes écran éteint, désactivez l’optimisation de batterie.',
                'Désactiver', () async {
              await FlutterForegroundTask.requestIgnoreBatteryOptimization();
              await _checkPermissions();
            }),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: SegmentedButton<bool>(
              segments: [
                ButtonSegment(value: false, label: Text('À traiter (${todo.length})')),
                ButtonSegment(value: true, label: Text('Toutes (${all.length})')),
              ],
              selected: {_showAll},
              onSelectionChanged: (s) => setState(() => _showAll = s.first),
            ),
          ),
          Expanded(
            child: shown.isEmpty
                ? Center(child: Text(_data == null ? 'Connexion…' : 'Aucune alerte', style: const TextStyle(color: Colors.black54)))
                : ListView.builder(
                    padding: const EdgeInsets.fromLTRB(12, 0, 12, 24),
                    itemCount: shown.length,
                    itemBuilder: (c, i) => AlertCard(alert: shown[i], api: _api, onChanged: _refresh),
                  ),
          ),
        ],
      ),
    );
  }
}

class _Banner extends StatelessWidget {
  final String text;
  final String action;
  final VoidCallback onTap;
  const _Banner(this.text, this.action, this.onTap);
  @override
  Widget build(BuildContext context) {
    return Container(
      color: const Color(0xFFF6E3B4),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Row(children: [
        Expanded(child: Text(text, style: const TextStyle(fontSize: 13, color: Color(0xFF6B4500)))),
        TextButton(onPressed: onTap, child: Text(action)),
      ]),
    );
  }
}

