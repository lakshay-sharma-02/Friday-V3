/**
 * Friday Companion — push notification registration.
 *
 * Pairing a phone binds its Expo push token to the device registry on the
 * desktop (``friday4 mobile pair`` prints the one-time code). From then on
 * the daemon's MobilePushWorker fans ambient events out to the phone via
 * the Expo push service (``expo_transporter`` on the server side).
 *
 * Requirements:
 *  - A physical device (simulators cannot receive remote push).
 *  - ``extra.eas.projectId`` in app.json set to your EAS project id.
 *  - A development build (Expo Go no longer runs expo-notifications).
 */
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import Constants from 'expo-constants';
import { Platform } from 'react-native';

const PROJECT_ID_PLACEHOLDER = 'REPLACE_WITH_YOUR_EAS_PROJECT_ID';

// Show ambient events as system notifications while the app is foregrounded.
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: false,
    shouldSetBadge: false,
  }),
});

export interface PushStatus {
  ok: boolean;
  token?: string;
  error?: string;
}

/** Android needs an explicit channel for notifications to surface. */
async function ensureChannel(): Promise<void> {
  if (Platform.OS !== 'android') {
    return;
  }
  try {
    await Notifications.setNotificationChannelAsync('ambient', {
      name: 'Friday ambient',
      importance: Notifications.AndroidImportance.HIGH,
      vibrationPattern: [0, 250, 250, 250],
      lightColor: '#1b6dff',
    });
  } catch {
    // channel setup failing must never break pairing
  }
}

/**
 * Resolve this phone's Expo push token, or a plain-language reason why
 * push is unavailable (shown on the Devices screen).
 */
export async function getPushToken(): Promise<PushStatus> {
  try {
    if (!Device.isDevice) {
      return {
        ok: false,
        error:
          'Remote push needs a physical device — simulators cannot receive it.',
      };
    }
    let status = (await Notifications.getPermissionsAsync()).status;
    if (status !== 'granted') {
      status = (await Notifications.requestPermissionsAsync()).status;
    }
    if (status !== 'granted') {
      return { ok: false, error: 'Notification permission was not granted.' };
    }
    await ensureChannel();

    const projectId = Constants.expoConfig?.extra?.eas?.projectId;
    if (!projectId || projectId === PROJECT_ID_PLACEHOLDER) {
      return {
        ok: false,
        error:
          'Set extra.eas.projectId in app.json to your EAS project id to receive push.',
      };
    }
    const { data } = await Notifications.getExpoPushTokenAsync({ projectId });
    return { ok: true, token: data };
  } catch (err) {
    return {
      ok: false,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}
