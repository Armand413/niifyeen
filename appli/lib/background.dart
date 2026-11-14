import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import 'api.dart';

const alertChannel = AndroidNotificationChannel(
  'niifyeen_alertes',
  'Alertes de cible',
  description: 'Alerte sonore quand une cible est détectée près de vous.',
  importance: Importance.max,
  playSound: true,
  enableVibration: true,
);

Future<void> initNotifications(FlutterLocalNotificationsPlugin plugin) async {
  await plugin.initialize(
    const InitializationSettings(android: AndroidInitializationSettings('@mipmap/ic_launcher')),
  );
  await plugin
      .resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>()
      ?.createNotificationChannel(alertChannel);
}

/// Interroge la boîte de réception et notifie les alertes non vues. Appelée toutes les 3 s par le service.
Future<void> pollOnce(FlutterLocalNotificationsPlugin plugin) async {
  final link = await Store.loadLink();
  if (link == null) return;
  final InboxData data;
  try {
    data = await InboxApi(link).fetch();
  } catch (_) {
    return; // réseau indisponible : on réessaie au prochain tour
  }
  if (data.alerts.isEmpty) return;

  final newest = data.alerts.map((a) => a.id).reduce((a, b) => a > b ? a : b);
  final last = await Store.loadLastNotified();
  if (last == null) {
    // Première connexion : on ne notifie pas l'historique.
    await Store.saveLastNotified(newest);
    return;
  }
  final fresh = data.alerts.where((a) => a.id > last && !a.acknowledged).toList()
    ..sort((a, b) => a.id.compareTo(b.id));
  for (final a in fresh) {
    final where = [a.camera, a.place].where((s) => s.isNotEmpty).join(' · ');
    final body = '$where\n${formatTime(a.createdAt)}';
    await plugin.show(
      a.id,
      'Cible détectée : ${a.target.isEmpty ? 'inconnue' : a.target}',
      body,
      NotificationDetails(
        android: AndroidNotificationDetails(
          alertChannel.id,
          alertChannel.name,
          channelDescription: alertChannel.description,
          importance: Importance.max,
          priority: Priority.high,
          playSound: true,
          enableVibration: true,
          category: AndroidNotificationCategory.alarm,
          styleInformation: BigTextStyleInformation(body),
        ),
      ),
    );
  }
  if (newest > last) await Store.saveLastNotified(newest);
}

@pragma('vm:entry-point')
void startCallback() {
  FlutterForegroundTask.setTaskHandler(PollHandler());
}

class PollHandler extends TaskHandler {
  final FlutterLocalNotificationsPlugin _plugin = FlutterLocalNotificationsPlugin();

  @override
  Future<void> onStart(DateTime timestamp, TaskStarter starter) async {
    await initNotifications(_plugin);
  }

  @override
  void onRepeatEvent(DateTime timestamp) {
    pollOnce(_plugin);
  }

  @override
  Future<void> onDestroy(DateTime timestamp, bool isTimeout) async {}
}

/// Démarre (ou redémarre) le service de réception en arrière-plan.
Future<void> startBackgroundService() async {
  FlutterForegroundTask.init(
    androidNotificationOptions: AndroidNotificationOptions(
      channelId: 'niifyeen_service',
      channelName: 'Réception des alertes',
      channelDescription: 'Maintient la connexion avec le serveur NiifYéén.',
      channelImportance: NotificationChannelImportance.LOW,
      priority: NotificationPriority.LOW,
    ),
    iosNotificationOptions: const IOSNotificationOptions(),
    foregroundTaskOptions: ForegroundTaskOptions(
      eventAction: ForegroundTaskEventAction.repeat(3000),
      autoRunOnBoot: true,
      allowWakeLock: true,
      allowWifiLock: true,
    ),
  );
  if (await FlutterForegroundTask.isRunningService) return;
  await FlutterForegroundTask.startService(
    serviceId: 300,
    serviceTypes: [ForegroundServiceTypes.specialUse],
    notificationTitle: 'NiifYéén : réception active',
    notificationText: 'En attente d’alertes',
    callback: startCallback,
  );
}

Future<void> stopBackgroundService() async {
  if (await FlutterForegroundTask.isRunningService) {
    await FlutterForegroundTask.stopService();
  }
}
