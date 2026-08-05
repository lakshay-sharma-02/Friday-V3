/**
 * Feed — the ambient event stream, live on the phone.
 *
 * Every event the daemon publishes (security scans, suggestions, collab
 * observations, mission progress) flows over the SSE durable queue into
 * this list. Priority colors mirror the bus: critical red, high amber,
 * normal green.
 */
import { FlatList, StyleSheet, Text, View } from 'react-native';
import type { AmbientEvent } from '../api';
import { priorityColor, theme } from '../theme';

interface Props {
  events: AmbientEvent[];
  connected: boolean;
}

function payloadSummary(payload: unknown): string {
  if (payload == null) return '';
  if (typeof payload === 'string') return payload;
  if (typeof payload === 'object') {
    const obj = payload as Record<string, unknown>;
    const value = obj.message ?? obj.summary ?? obj.title;
    if (typeof value === 'string') return value;
    return JSON.stringify(obj).slice(0, 120);
  }
  return String(payload);
}

export function FeedScreen({ events, connected }: Props) {
  const data = [...events].reverse(); // newest first
  return (
    <View style={styles.container}>
      <View style={styles.headerRow}>
        <Text style={styles.count}>{data.length} events</Text>
        <View style={[styles.live, connected ? styles.liveOn : styles.liveOff]}>
          <Text style={styles.liveText}>{connected ? '● live' : '○ reconnecting'}</Text>
        </View>
      </View>
      <FlatList
        data={data}
        keyExtractor={(item) => String(item.id)}
        contentContainerStyle={styles.list}
        renderItem={({ item }) => (
          <View style={styles.card}>
            <View style={styles.rowBetween}>
              <Text style={styles.topic}>{item.topic ?? 'ambient'}</Text>
              <View
                style={[styles.chip, { borderColor: priorityColor(item.priority) }]}
              >
                <Text style={[styles.chipText, { color: priorityColor(item.priority) }]}>
                  {item.priority ?? 'info'}
                </Text>
              </View>
            </View>
            {payloadSummary(item.payload) ? (
              <Text style={styles.body}>{payloadSummary(item.payload)}</Text>
            ) : null}
            <Text style={styles.dim}>
              {item.source ?? 'friday'} · {item.created_at ?? ''}
            </Text>
          </View>
        )}
        ListEmptyComponent={
          <View style={styles.empty}>
            <Text style={styles.emptyTitle}>Quiet so far</Text>
            <Text style={styles.dim}>
              New ambient events from the daemon will appear here the moment
              they happen.
            </Text>
          </View>
        }
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: theme.spacing,
    paddingTop: theme.spacing,
  },
  count: { color: theme.textDim, fontSize: 12, fontWeight: '600' },
  live: { paddingHorizontal: 10, paddingVertical: 3, borderRadius: 999, borderWidth: 1 },
  liveOn: { borderColor: theme.green },
  liveOff: { borderColor: theme.border },
  liveText: { color: theme.text, fontSize: 12 },
  list: { padding: theme.spacing, gap: 8 },
  card: {
    backgroundColor: theme.surface,
    borderRadius: theme.radius,
    borderWidth: 1,
    borderColor: theme.border,
    padding: theme.spacing,
    gap: 6,
  },
  rowBetween: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  topic: { color: theme.text, fontWeight: '700', fontSize: 14 },
  chip: { borderWidth: 1, borderRadius: 999, paddingHorizontal: 8, paddingVertical: 2 },
  chipText: { fontSize: 11, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.6 },
  body: { color: theme.text, fontSize: 13, lineHeight: 19 },
  dim: { color: theme.textDim, fontSize: 11 },
  empty: { paddingTop: 60, alignItems: 'center', gap: 6 },
  emptyTitle: { color: theme.text, fontWeight: '700', fontSize: 15 },
});
