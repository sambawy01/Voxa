/** Every voice Kokoro v1.0 ships, with the quality grade the authors publish.
 *
 * Grades come from hexgrad/Kokoro-82M VOICES.md — an honest signal, so the UI can
 * show which voices are actually good rather than pretending they are all equal.
 * (French really is one voice; Spanish three. That is the model, not an omission.)
 */
export const LANGUAGES = [
  { code: 'a', label: 'English (US)', flag: '🇺🇸' },
  { code: 'b', label: 'English (UK)', flag: '🇬🇧' },
  { code: 'e', label: 'Spanish', flag: '🇪🇸' },
  { code: 'f', label: 'French', flag: '🇫🇷' },
  { code: 'h', label: 'Hindi', flag: '🇮🇳' },
  { code: 'i', label: 'Italian', flag: '🇮🇹' },
  { code: 'j', label: 'Japanese', flag: '🇯🇵' },
  { code: 'p', label: 'Portuguese (BR)', flag: '🇧🇷' },
  { code: 'z', label: 'Chinese (Mandarin)', flag: '🇨🇳' },
];

/** voice id -> { grade, traits } ; grade '' means the card did not grade it */
export const VOICES = {
  // American English
  af_heart: { grade: 'A', traits: 'warm, expressive' },
  af_bella: { grade: 'A-', traits: 'expressive, bright' },
  af_nicole: { grade: 'B-', traits: 'soft, close' },
  af_aoede: { grade: 'C+', traits: 'clear' },
  af_kore: { grade: 'C+', traits: 'even' },
  af_sarah: { grade: 'C+', traits: 'neutral' },
  af_alloy: { grade: 'C', traits: 'standard' },
  af_nova: { grade: 'C', traits: 'standard' },
  af_sky: { grade: 'C-', traits: 'light' },
  af_jessica: { grade: 'D', traits: 'thin data' },
  af_river: { grade: 'D', traits: 'thin data' },
  am_fenrir: { grade: 'C+', traits: 'grounded' },
  am_michael: { grade: 'C+', traits: 'steady' },
  am_puck: { grade: 'C+', traits: 'lively' },
  am_adam: { grade: 'D', traits: 'thin data' },
  am_echo: { grade: 'D', traits: 'thin data' },
  am_eric: { grade: 'D', traits: 'thin data' },
  am_liam: { grade: 'D', traits: 'thin data' },
  am_onyx: { grade: 'D', traits: 'thin data' },
  am_santa: { grade: 'D-', traits: 'character' },
  // British English
  bf_emma: { grade: 'B-', traits: 'clear, polite' },
  bf_isabella: { grade: 'C', traits: 'measured' },
  bf_alice: { grade: 'D', traits: 'thin data' },
  bf_lily: { grade: 'D', traits: 'thin data' },
  bm_fable: { grade: 'C', traits: 'narration' },
  bm_george: { grade: 'C', traits: 'steady' },
  bm_lewis: { grade: 'D+', traits: 'light' },
  bm_daniel: { grade: 'D', traits: 'thin data' },
  // Spanish
  ef_dora: { grade: '', traits: 'female' },
  em_alex: { grade: '', traits: 'male' },
  em_santa: { grade: '', traits: 'male, character' },
  // French
  ff_siwis: { grade: 'B-', traits: 'the French voice' },
  // Hindi
  hf_alpha: { grade: 'C', traits: 'female' },
  hf_beta: { grade: 'C', traits: 'female' },
  hm_omega: { grade: 'C', traits: 'male' },
  hm_psi: { grade: 'C', traits: 'male' },
  // Italian
  if_sara: { grade: 'C', traits: 'female' },
  im_nicola: { grade: 'C', traits: 'male' },
  // Japanese
  jf_alpha: { grade: 'C+', traits: 'female' },
  jf_gongitsune: { grade: 'C', traits: 'storytelling' },
  jf_tebukuro: { grade: 'C', traits: 'female' },
  jf_nezumi: { grade: 'C-', traits: 'female' },
  jm_kumo: { grade: 'C-', traits: 'male' },
  // Portuguese (BR)
  pf_dora: { grade: '', traits: 'female' },
  pm_alex: { grade: '', traits: 'male' },
  pm_santa: { grade: '', traits: 'male, character' },
  // Chinese (Mandarin)
  zf_xiaobei: { grade: 'D', traits: 'thin data' },
  zf_xiaoni: { grade: 'D', traits: 'thin data' },
  zf_xiaoxiao: { grade: 'D', traits: 'thin data' },
  zf_xiaoyi: { grade: 'D', traits: 'thin data' },
  zm_yunjian: { grade: 'D', traits: 'thin data' },
  zm_yunxi: { grade: 'D', traits: 'thin data' },
  zm_yunxia: { grade: 'D', traits: 'thin data' },
  zm_yunyang: { grade: 'D', traits: 'thin data' },
};

/** the female/male split is encoded in the second character of the id */
export function voiceGender(id) {
  const c = id[1];
  return c === 'f' ? 'female' : c === 'm' ? 'male' : '';
}

export function voicesFor(langCode) {
  return Object.keys(VOICES)
    .filter((v) => v[0] === langCode)
    .sort((a, b) => {
      const ga = VOICES[a].grade || 'Z';
      const gb = VOICES[b].grade || 'Z';
      return ga.localeCompare(gb);
    });
}
