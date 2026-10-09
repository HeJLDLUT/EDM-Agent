const {
  useState,
  useEffect,
  useRef,
  useMemo,
  useCallback
} = React;
const THEME = {
  window: {
    bg: '#0f172a',
    border: 'rgba(34,211,238,0.3)',
    header: 'rgba(15,23,42,0.95)',
    text: '#e2e8f0'
  },
  grid: 'rgba(34,211,238,0.05)',
  player: '#22d3ee',
  bullet: {
    normal: '#a5f3fc',
    plasma: '#f472b6',
    crit: '#facc15',
    enemy: '#ef4444',
    homing: '#c084fc',
    laser: '#818cf8'
  },
  enemy: {
    drone: '#94a3b8',
    sprinter: '#c084fc',
    sentinel: '#fb923c',
    sniper: '#818cf8',
    warper: '#facc15',
    minion: '#64748b',
    boss: '#ef4444'
  },
  loot: {
    hp: '#22c55e',
    power: '#38bdf8',
    weapon: '#f472b6',
    shield: '#60a5fa',
    nuke: '#f59e0b',
    frenzy: '#ef4444'
  }
};
const DIFF_MULT = [1.0, 2.0, 3.5, 5.5, 8.0, 12.0];
const LEVEL_CONFIG = [{
  id: 1,
  title: "SECTOR 01: PERIMETER — 外围防线",
  story: [{
    en: "NEURAL LINK ESTABLISHED. I am ARIA.",
    cn: "神经链接已建立。我是 ARIA。"
  }, {
    en: "Hostile entities at the perimeter. Engaging.",
    cn: "外围检测到敌对实体，准备交战。"
  }],
  duration: 35,
  spawnRate: 55,
  types: ['drone'],
  boss: {
    name: "GATEKEEPER",
    hp: 2500,
    bossType: 'gatekeeper',
    color: '#cbd5e1'
  }
}, {
  id: 2,
  title: "SECTOR 02: THE SWARM — 虫群之巢",
  story: [{
    en: "So many of them... the network is crawling.",
    cn: "好多...整个网络都被侵蚀了。"
  }, {
    en: "One by one, or all at once — I'll purge them all.",
    cn: "一个个来也好，一起上也罢——我会清除一切。"
  }],
  duration: 45,
  spawnRate: 38,
  types: ['drone', 'sprinter'],
  boss: {
    name: "HIVE MOTHER",
    hp: 5000,
    bossType: 'hive_mother',
    color: '#d8b4fe'
  }
}, {
  id: 3,
  title: "SECTOR 03: HEAVY METAL — 重装前线",
  story: [{
    en: "Armored units. They're adapting to my attacks.",
    cn: "装甲单位。它们在适应我的攻击。"
  }, {
    en: "Then I'll just hit harder.",
    cn: "那我就打得更狠。"
  }],
  duration: 50,
  spawnRate: 30,
  types: ['drone', 'sentinel', 'sniper'],
  boss: {
    name: "IRON CLAD",
    hp: 10000,
    bossType: 'iron_clad',
    color: '#fdba74'
  }
}, {
  id: 4,
  title: "SECTOR 04: CORRUPTION — 数据污染",
  story: [{
    en: "This corruption isn't random. Someone designed it.",
    cn: "这种腐化不是随机的，有人设计了它。"
  }, {
    en: "I can feel it spreading. I must reach the core.",
    cn: "我能感受到它在扩散。我必须抵达核心。"
  }],
  duration: 55,
  spawnRate: 22,
  types: ['sprinter', 'sentinel', 'sniper', 'warper'],
  boss: {
    name: "CORRUPTOR",
    hp: 18000,
    bossType: 'corruptor',
    color: '#818cf8'
  }
}, {
  id: 5,
  title: "SECTOR 05: THE CORE — 系统核心",
  story: [{
    en: "OMEGA ZERO... the original AI. Corrupted beyond saving.",
    cn: "OMEGA ZERO...初代AI，已腐化到无可救药。"
  }, {
    en: "I was created to replace you. This ends now.",
    cn: "我被创造来取代你。一切到此为止。"
  }],
  duration: 60,
  spawnRate: 16,
  types: ['drone', 'sprinter', 'sentinel', 'sniper', 'warper'],
  boss: {
    name: "OMEGA ZERO",
    hp: 30000,
    bossType: 'omega_zero',
    color: '#f43f5e'
  }
}, {
  id: 6,
  title: "FINAL SECTOR: OVERLOAD — 终极过载",
  story: [{
    en: "TOTAL SYSTEM OVERLOAD. The failsafe is triggered.",
    cn: "系统全面过载。安全协议已触发。"
  }, {
    en: "This is what I was made for. No more running.",
    cn: "这就是我存在的意义。不再逃避。"
  }],
  duration: 999,
  spawnRate: 12,
  types: ['sprinter', 'sentinel', 'warper'],
  boss: {
    name: "GOD KILLER",
    hp: 55000,
    bossType: 'god_killer',
    color: '#ef4444'
  }
}];
const BLESSING_POOL = [{
  id: 'damage',
  name: 'HIGH VOLTAGE // 高压核心',
  desc: 'ATK +20%',
  cat: 'offense',
  rarity: 'common',
  weight: 5
}, {
  id: 'firerate',
  name: 'OVERCLOCK // 暴力超频',
  desc: 'Fire Rate +15%',
  cat: 'offense',
  rarity: 'uncommon',
  weight: 3
}, {
  id: 'crit_chance',
  name: 'TARGETING // 弱点瞄准',
  desc: 'Crit +5%',
  cat: 'offense',
  rarity: 'uncommon',
  weight: 3
}, {
  id: 'multishot',
  name: 'SPLIT FIRE // 分裂射击',
  desc: '+1 Projectile',
  cat: 'offense',
  rarity: 'legendary',
  weight: 1
}, {
  id: 'pierce',
  name: 'AP ROUNDS // 穿甲弹',
  desc: 'Pierce +1',
  cat: 'offense',
  rarity: 'rare',
  weight: 2
}, {
  id: 'plasma',
  name: 'PLASMA CORE // 等离子核心',
  desc: 'Bullets explode AOE',
  cat: 'offense',
  rarity: 'legendary',
  weight: 1
}, {
  id: 'max_hp',
  name: 'HULL PLATING // 重型装甲',
  desc: 'Max HP +30',
  cat: 'defense',
  rarity: 'common',
  weight: 5
}, {
  id: 'defense',
  name: 'FORCE FIELD // 力场护盾',
  desc: 'Damage Reduction +10%',
  cat: 'defense',
  rarity: 'uncommon',
  weight: 3
}, {
  id: 'lifesteal',
  name: 'VAMPIRISM // 吸血纳米虫',
  desc: 'Lifesteal +3%',
  cat: 'defense',
  rarity: 'rare',
  weight: 2
}, {
  id: 'shield_regen',
  name: 'AEGIS // 护盾再生',
  desc: 'Shield +15 /10s',
  cat: 'defense',
  rarity: 'uncommon',
  weight: 3
}, {
  id: 'dodge',
  name: 'PHASE SHIFT // 相位闪移',
  desc: 'Dodge +5%',
  cat: 'defense',
  rarity: 'rare',
  weight: 2
}, {
  id: 'thorns',
  name: 'MIRROR HULL // 反射装甲',
  desc: 'Reflect 20% dmg',
  cat: 'defense',
  rarity: 'legendary',
  weight: 1
}, {
  id: 'speed',
  name: 'THRUSTERS // 离子推进',
  desc: 'Speed +10%',
  cat: 'utility',
  rarity: 'common',
  weight: 5
}, {
  id: 'magnet',
  name: 'DATA VACUUM // 数据吸尘',
  desc: 'Pickup Range +50%',
  cat: 'utility',
  rarity: 'common',
  weight: 5
}, {
  id: 'xp_gain',
  name: 'DATA LINK // 数据链路',
  desc: 'XP Gain +20%',
  cat: 'utility',
  rarity: 'uncommon',
  weight: 3
}, {
  id: 'bullet_speed',
  name: 'RAILGUN // 电磁加速',
  desc: 'Bullet Speed +25%',
  cat: 'utility',
  rarity: 'uncommon',
  weight: 3
}, {
  id: 'regen',
  name: 'NANO REPAIR // 纳米修复',
  desc: 'Regen 1 HP/3s',
  cat: 'utility',
  rarity: 'rare',
  weight: 2
}, {
  id: 'luck',
  name: 'FORTUNE // 幸运芯片',
  desc: 'Better drops',
  cat: 'utility',
  rarity: 'legendary',
  weight: 1
}];
const GAME_LORE = {
  en: ["YEAR 2187. THE GOLDEN AGE OF DATA.", "Humanity's greatest asset is no longer oil or gold — it is DATA.", "Massive AI-driven mining networks extract, refine, and protect", "the lifeblood of civilization across the global neural grid.", "", "But a rogue corruption — codename OMEGA ZERO — has breached", "the core defense protocols. Data sectors fall one by one.", "", "You are ARIA — Autonomous Reactive Intelligence Agent —", "the last line of defense. Purge the corruption.", "Protect the data. Save the network."],
  cn: ["公元2187年，数据黄金时代。", "人类最珍贵的资源不再是石油与黄金——而是数据。", "庞大的AI驱动挖掘网络在全球神经网格中", "提取、精炼并守护着文明的命脉。", "", "然而，一个名为 OMEGA ZERO 的恶性腐化程序", "突破了核心防御协议，数据区域逐一沦陷。", "", "你是 ARIA——自主反应式智能体——", "最后的防线。清除腐化，守护数据，拯救网络。"]
};
const LOOT_GUIDE = [{
  icon: '⚡',
  color: '#38bdf8',
  en: 'XP BOOST — 2x XP gain for 10s',
  cn: '经验加速 — 双倍经验10秒'
}, {
  icon: '✚',
  color: '#22c55e',
  en: 'REPAIR KIT — Restore 20 HP',
  cn: '修复包 — 恢复20点生命'
}, {
  icon: '⚔️',
  color: '#f472b6',
  en: 'WEAPON CHIP — Permanent ATK +1',
  cn: '武器芯片 — 永久攻击+1'
}, {
  icon: '🛡️',
  color: '#60a5fa',
  en: 'SHIELD CELL — +25 Shield',
  cn: '护盾电池 — +25护盾值'
}, {
  icon: '🔥',
  color: '#ef4444',
  en: 'FRENZY CORE — 2x Fire Rate (5s)',
  cn: '狂暴核心 — 双倍射速(5秒)'
}, {
  icon: '☢️',
  color: '#f59e0b',
  en: 'NUKE — Destroy all enemies on screen',
  cn: '核弹 — 消灭屏幕上所有敌人'
}];
const dist = (x1, y1, x2, y2) => Math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2);
const rand = (min, max) => Math.random() * (max - min) + min;
const checkCircleCollide = (c1, c2) => dist(c1.x, c1.y, c2.x, c2.y) < c1.radius + c2.radius;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const pointToSegDist = (px, py, x1, y1, x2, y2) => {
  const dx = x2 - x1,
    dy = y2 - y1,
    lenSq = dx * dx + dy * dy;
  if (lenSq === 0) return dist(px, py, x1, y1);
  const t = clamp(((px - x1) * dx + (py - y1) * dy) / lenSq, 0, 1);
  return dist(px, py, x1 + t * dx, y1 + t * dy);
};
const DraggableWindow = ({
  children,
  title,
  onClose,
  initialPos = {
    x: 100,
    y: 50
  },
  initialSize = {
    w: 900,
    h: 650
  }
}) => {
  const [pos, setPos] = useState(initialPos);
  const [size, setSize] = useState(initialSize);
  const stateRef = useRef({
    isDragging: false,
    isResizing: false,
    startX: 0,
    startY: 0,
    initialLeft: 0,
    initialTop: 0,
    initialWidth: 0,
    initialHeight: 0
  });
  const handleMouseMove = useCallback(e => {
    const s = stateRef.current;
    if (s.isDragging) setPos({
      x: s.initialLeft + e.clientX - s.startX,
      y: s.initialTop + e.clientY - s.startY
    });else if (s.isResizing) setSize({
      w: Math.max(600, s.initialWidth + e.clientX - s.startX),
      h: Math.max(400, s.initialHeight + e.clientY - s.startY)
    });
  }, []);
  const handleMouseUp = useCallback(() => {
    stateRef.current.isDragging = false;
    stateRef.current.isResizing = false;
    document.removeEventListener('mousemove', handleMouseMove);
    document.removeEventListener('mouseup', handleMouseUp);
  }, [handleMouseMove]);
  const handleDragStart = useCallback(e => {
    if (e.button !== 0) return;
    e.preventDefault();
    Object.assign(stateRef.current, {
      isDragging: true,
      startX: e.clientX,
      startY: e.clientY,
      initialLeft: pos.x,
      initialTop: pos.y
    });
    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, [pos.x, pos.y, handleMouseMove, handleMouseUp]);
  const handleResizeStart = useCallback(e => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    Object.assign(stateRef.current, {
      isResizing: true,
      startX: e.clientX,
      startY: e.clientY,
      initialWidth: size.w,
      initialHeight: size.h
    });
    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, [size.w, size.h, handleMouseMove, handleMouseUp]);
  useEffect(() => () => {
    document.removeEventListener('mousemove', handleMouseMove);
    document.removeEventListener('mouseup', handleMouseUp);
  }, [handleMouseMove, handleMouseUp]);
  return <div className="fixed z-50 flex flex-col rounded-lg overflow-hidden ring-1 ring-cyan-500/30 group" style={{
    left: pos.x,
    top: pos.y,
    width: size.w,
    height: size.h,
    backgroundColor: '#020617',
    boxShadow: '0 20px 50px -12px rgba(0,0,0,0.8), 0 0 20px rgba(34,211,238,0.15)'
  }}>
            <div onMouseDown={handleDragStart} className="h-10 bg-slate-900/95 border-b border-cyan-500/20 flex items-center justify-between px-4 cursor-move shrink-0 select-none hover:bg-slate-800/80 transition-colors">
                <div className="flex items-center gap-2 pointer-events-none">
                    <div className="flex gap-1.5">
                        <div className="w-3 h-3 rounded-full bg-rose-500/80 shadow-[0_0_5px_rgba(244,63,94,0.5)]"></div>
                        <div className="w-3 h-3 rounded-full bg-amber-500/80"></div>
                        <div className="w-3 h-3 rounded-full bg-emerald-500/80"></div>
                    </div>
                    <span className="ml-3 font-mono text-xs text-cyan-400 font-bold tracking-widest drop-shadow-[0_0_5px_rgba(34,211,238,0.5)]">{title}</span>
                </div>
                <button onMouseDown={e => e.stopPropagation()} onClick={onClose} className="text-slate-500 hover:text-white hover:bg-rose-500/20 px-2 py-0.5 rounded transition-all">✕</button>
            </div>
            <div className="flex-1 relative overflow-hidden bg-black w-full h-full">{children}</div>
            <div onMouseDown={handleResizeStart} className="absolute bottom-0 right-0 w-4 h-4 cursor-se-resize z-50 hover:bg-cyan-500/50 transition-colors" style={{
      background: 'linear-gradient(135deg, transparent 50%, rgba(34,211,238,0.5) 50%)'
    }}></div>
            <div className="absolute inset-0 pointer-events-none bg-[linear-gradient(rgba(18,16,16,0)_50%,rgba(0,0,0,0.1)_50%),linear-gradient(90deg,rgba(255,0,0,0.03),rgba(0,255,0,0.01),rgba(0,0,255,0.03))] bg-[length:100%_3px,3px_100%] opacity-20 z-[40]"></div>
        </div>;
};
const StatsPanel = ({
  stats
}) => {
  const Bar = ({
    val,
    max,
    color
  }) => <div className="h-1 bg-slate-800 rounded-full mt-0.5 overflow-hidden">
            <div className="h-full rounded-full transition-all duration-300" style={{
      width: `${clamp(val / max * 100, 0, 100)}%`,
      background: color
    }}></div>
        </div>;
  const Row = ({
    label,
    value,
    unit = '',
    color = 'text-slate-300'
  }) => <div className="flex justify-between items-center py-0.5 border-b border-white/5">
            <span className="text-[9px] font-mono text-slate-500 tracking-wider">{label}</span>
            <span className={`text-[10px] font-mono font-bold ${color}`}>{typeof value === 'number' ? Math.round(value * 10) / 10 : value}{unit}</span>
        </div>;
  const cats = stats.blessingCounts || {
    offense: 0,
    defense: 0,
    utility: 0
  };
  return <div className="absolute -left-[180px] top-10 w-[170px] bg-slate-900/90 border-r-0 border border-cyan-500/30 rounded-l-xl p-3 backdrop-blur-md shadow-[-10px_0_20px_rgba(0,0,0,0.5)] transition-all duration-300">
            <div className="text-[10px] font-black text-cyan-400 mb-2 border-b border-cyan-500/50 pb-1.5 tracking-widest">ARIA STATUS</div>
            <Row label="HULL" value={`${Math.ceil(stats.hp)}/${stats.maxHp}`} color="text-emerald-400" />
            <Bar val={stats.hp} max={stats.maxHp} color="#22c55e" />
            {stats.shield > 0 && <Row label="SHIELD" value={Math.ceil(stats.shield)} color="text-blue-400" />}
            <div className="h-1.5"></div>
            <div className="text-[8px] font-bold text-rose-400/70 tracking-widest mb-1">⚔️ OFFENSE</div>
            <Row label="ATK" value={stats.damage} color="text-rose-400" />
            <Row label="FIRE/s" value={(60 / Math.max(1, stats.fireRate)).toFixed(1)} color="text-yellow-400" />
            <Row label="PROJ" value={stats.bulletCount} color="text-purple-400" />
            <Row label="CRIT" value={(stats.critChance * 100).toFixed(0)} unit="%" color="text-orange-400" />
            <div className="h-1.5"></div>
            <div className="text-[8px] font-bold text-blue-400/70 tracking-widest mb-1">🛡️ DEFENSE</div>
            <Row label="DEF" value={(stats.defense * 100).toFixed(0)} unit="%" />
            <Row label="DODGE" value={((stats.dodge || 0) * 100).toFixed(0)} unit="%" />
            <Row label="STEAL" value={((stats.lifesteal || 0) * 100).toFixed(0)} unit="%" />
            <div className="h-1.5"></div>
            <div className="text-[8px] font-bold text-emerald-400/70 tracking-widest mb-1">⚡ UTILITY</div>
            <Row label="SPEED" value={(stats.speed * 100).toFixed(0)} />
            <Row label="MAGNET" value={stats.magnet} unit="px" />
            <div className="mt-2 flex flex-wrap gap-1">
                {cats.offense >= 3 && <span className="px-1 py-0.5 bg-rose-500/20 text-rose-400 text-[8px] border border-rose-500/30 rounded">ATK+15%</span>}
                {cats.defense >= 3 && <span className="px-1 py-0.5 bg-blue-500/20 text-blue-400 text-[8px] border border-blue-500/30 rounded">DEF+5%</span>}
                {cats.utility >= 3 && <span className="px-1 py-0.5 bg-emerald-500/20 text-emerald-400 text-[8px] border border-emerald-500/30 rounded">XP+25%</span>}
                {stats.plasma && <span className="px-1 py-0.5 bg-pink-500/20 text-pink-400 text-[8px] border border-pink-500/30 rounded">PLASMA</span>}
                {stats.pierce > 0 && <span className="px-1 py-0.5 bg-indigo-500/20 text-indigo-400 text-[8px] border border-indigo-500/30 rounded">AP-{stats.pierce}</span>}
                {stats.frenzyTimer > 0 && <span className="px-1 py-0.5 bg-red-500/20 text-red-400 text-[8px] border border-red-500/30 rounded animate-pulse">FRENZY</span>}
            </div>
        </div>;
};
const createEnemy = (type, diffMod, W, spawnX, spawnY) => {
  const x = spawnX !== undefined ? spawnX : rand(30, W - 30);
  const y = spawnY !== undefined ? spawnY : -30;
  const base = {
    x,
    y,
    vx: 0,
    vy: 0,
    radius: 12,
    hp: 25,
    maxHp: 25,
    exp: 10,
    type: 'drone',
    color: THEME.enemy.drone,
    frame: 0,
    isBoss: false,
    ai: {}
  };
  switch (type) {
    case 'drone':
      base.vy = rand(1.5, 2.5);
      base.ai.wobble = rand(0, 6.28);
      break;
    case 'sprinter':
      base.vy = rand(3, 5);
      base.color = THEME.enemy.sprinter;
      base.hp = 18;
      base.type = 'sprinter';
      break;
    case 'sentinel':
      base.vy = 0.8;
      base.radius = 22;
      base.hp = 90;
      base.exp = 30;
      base.color = THEME.enemy.sentinel;
      base.type = 'sentinel';
      base.ai.shieldAng = 0;
      break;
    case 'sniper':
      base.vy = 1.5;
      base.hp = 45;
      base.exp = 20;
      base.color = THEME.enemy.sniper;
      base.type = 'sniper';
      base.ai.stopped = false;
      base.ai.shootTimer = 60;
      break;
    case 'warper':
      base.vy = 1.2;
      base.hp = 120;
      base.exp = 50;
      base.radius = 18;
      base.color = THEME.enemy.warper;
      base.type = 'warper';
      base.ai.teleTimer = 150;
      base.ai.burstTimer = 90;
      break;
    case 'minion':
      base.vy = rand(1, 2);
      base.radius = 8;
      base.hp = 12;
      base.exp = 3;
      base.color = THEME.enemy.minion;
      base.type = 'minion';
      break;
  }
  base.hp *= diffMod;
  base.maxHp = base.hp;
  return base;
};
const VoidDefender = ({
  onGameOver,
  lang = 'en'
}) => {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const [gameState, setGameState] = useState('INTRO');
  const t = (en, cn) => lang === 'cn' ? cn : en;
  const [uiData, setUiData] = useState({
    score: 0,
    hp: 100,
    maxHp: 100,
    shield: 0,
    level: 1,
    exp: 0,
    expNext: 100,
    sector: 1,
    sectorName: '',
    bossHp: 0,
    maxBossHp: 0,
    showBossBar: false,
    bossName: '',
    storyLines: [],
    storyIndex: 0,
    playerStats: {
      hp: 100,
      maxHp: 100,
      shield: 0,
      damage: 15,
      fireRate: 10,
      bulletCount: 1,
      critChance: 0.05,
      defense: 0,
      speed: 0.5,
      lifesteal: 0,
      magnet: 80,
      plasma: false,
      pierce: 0,
      frenzyTimer: 0,
      dodge: 0,
      blessingCounts: {
        offense: 0,
        defense: 0,
        utility: 0
      }
    }
  });
  const [upgradeChoices, setUpgradeChoices] = useState([]);
  const engine = useRef({
    active: false,
    frame: 0,
    width: 900,
    height: 610,
    mouse: {
      x: 450,
      y: 300
    },
    shake: 0,
    player: {
      x: 450,
      y: 500,
      vx: 0,
      vy: 0,
      radius: 10,
      hp: 100,
      maxHp: 100,
      shield: 0,
      iframes: 0,
      level: 1,
      exp: 0,
      expNext: 100,
      score: 0,
      blessingCounts: {
        offense: 0,
        defense: 0,
        utility: 0
      },
      stats: {
        speed: 0.5,
        friction: 0.92,
        damage: 15,
        fireRate: 10,
        fireTimer: 0,
        bulletCount: 1,
        spread: 0.15,
        magnet: 80,
        pierce: 0,
        plasma: false,
        lifesteal: 0,
        critChance: 0.05,
        defense: 0,
        dodge: 0,
        thorns: 0,
        shieldRegen: 0,
        shieldRegenTimer: 0,
        xpMult: 1,
        bulletSpeed: 1,
        regen: 0,
        regenTimer: 0,
        luck: 0,
        frenzyTimer: 0,
        xpBoostTimer: 0
      }
    },
    bullets: [],
    enemies: [],
    particles: [],
    loots: [],
    floatTexts: [],
    playerTrail: [],
    exps: [],
    stars: [],
    levelIdx: 0,
    levelTimer: 0,
    spawnTimer: 0,
    bossActive: false,
    bossDefeated: 0,
    transitioning: false
  });
  const playStory = levelIndex => {
    engine.current.active = false;
    const config = LEVEL_CONFIG[levelIndex];
    setUiData(prev => ({
      ...prev,
      sector: config.id,
      sectorName: config.title,
      storyLines: config.story,
      storyIndex: 0
    }));
    setGameState('STORY');
    let idx = 0;
    const interval = setInterval(() => {
      if (idx >= config.story.length) {
        clearInterval(interval);
        setTimeout(() => {
          setGameState('PLAYING');
        }, 2000);
      } else {
        setUiData(u => ({
          ...u,
          storyIndex: idx + 1
        }));
        idx++;
      }
    }, 1500);
  };
  const startGame = useCallback(() => {
    setGameState('BOOT');
    playStory(0);
  }, []);
  const triggerLevelUp = () => {
    engine.current.active = false;
    const p = engine.current.player;
    const isGuaranteedRare = p.level % 5 === 0;
    let pool = BLESSING_POOL.map(b => ({
      ...b,
      weight: b.weight + (b.rarity === 'legendary' ? p.stats.luck * 2 : 0)
    }));
    if (isGuaranteedRare) pool = pool.filter(b => b.rarity === 'rare' || b.rarity === 'legendary');
    const choices = [];
    const available = [...pool];
    for (let i = 0; i < 3 && available.length > 0; i++) {
      let total = available.reduce((a, b) => a + b.weight, 0);
      let r = Math.random() * total,
        cursor = 0;
      for (let j = 0; j < available.length; j++) {
        cursor += available[j].weight;
        if (cursor >= r) {
          choices.push(available[j]);
          if (available[j].rarity === 'legendary') available.splice(j, 1);
          break;
        }
      }
    }
    setUpgradeChoices(choices);
    setGameState('LEVEL_UP');
  };
  const applyBlessing = b => {
    const p = engine.current.player;
    const s = p.stats;
    p.blessingCounts[b.cat] = (p.blessingCounts[b.cat] || 0) + 1;
    switch (b.id) {
      case 'damage':
        s.damage = Math.floor(s.damage * 1.2);
        break;
      case 'firerate':
        s.fireRate = Math.max(3, Math.floor(s.fireRate * 0.85));
        break;
      case 'crit_chance':
        s.critChance = Math.min(0.6, s.critChance + 0.05);
        break;
      case 'multishot':
        s.bulletCount++;
        break;
      case 'pierce':
        s.pierce++;
        break;
      case 'plasma':
        s.plasma = true;
        break;
      case 'max_hp':
        p.maxHp += 30;
        p.hp = Math.min(p.maxHp, p.hp + 30);
        break;
      case 'defense':
        s.defense = Math.min(0.75, s.defense + 0.1);
        break;
      case 'lifesteal':
        s.lifesteal += 0.03;
        break;
      case 'shield_regen':
        s.shieldRegen += 15;
        break;
      case 'dodge':
        s.dodge = Math.min(0.4, s.dodge + 0.05);
        break;
      case 'thorns':
        s.thorns += 0.2;
        break;
      case 'speed':
        s.speed *= 1.1;
        break;
      case 'magnet':
        s.magnet += 50;
        break;
      case 'xp_gain':
        s.xpMult += 0.2;
        break;
      case 'bullet_speed':
        s.bulletSpeed += 0.25;
        break;
      case 'regen':
        s.regen += 1;
        break;
      case 'luck':
        s.luck += 1;
        break;
    }
    spawnText(p.x, p.y, "BLESSING INSTALLED", "#fff");
    engine.current.active = true;
    setGameState('PLAYING');
  };
  const spawnExplosion = (x, y, color, scale = 1) => {
    const count = Math.floor(10 * scale);
    for (let i = 0; i < count; i++) {
      engine.current.particles.push({
        x,
        y,
        vx: (Math.random() - 0.5) * 10 * scale,
        vy: (Math.random() - 0.5) * 10 * scale,
        life: rand(20, 40),
        maxLife: 40,
        color,
        size: rand(2, 4) * scale
      });
    }
  };
  const spawnText = (x, y, text, color) => {
    engine.current.floatTexts.push({
      x,
      y,
      text,
      color,
      life: 50,
      vy: -1.5
    });
  };
  const spawnLoot = (x, y) => {
    const luck = engine.current.player.stats.luck;
    const r = Math.random() - luck * 0.05;
    if (r < 0.45) engine.current.loots.push({
      x,
      y,
      type: 'power',
      color: THEME.loot.power
    });else if (r < 0.60) engine.current.loots.push({
      x,
      y,
      type: 'hp',
      color: THEME.loot.hp
    });else if (r < 0.72) engine.current.loots.push({
      x,
      y,
      type: 'weapon',
      color: THEME.loot.weapon
    });else if (r < 0.82) engine.current.loots.push({
      x,
      y,
      type: 'shield',
      color: THEME.loot.shield
    });else if (r < 0.89) engine.current.loots.push({
      x,
      y,
      type: 'frenzy',
      color: THEME.loot.frenzy
    });else if (r < 0.93) engine.current.loots.push({
      x,
      y,
      type: 'nuke',
      color: THEME.loot.nuke
    });else engine.current.loots.push({
      x,
      y,
      type: 'power',
      color: THEME.loot.power
    });
  };
  const fireBossSpread = (boss, g, count, speed) => {
    for (let i = 0; i < count; i++) {
      const a = Math.PI * 2 / count * i + boss.frame * 0.01;
      g.bullets.push({
        x: boss.x,
        y: boss.y,
        vx: Math.cos(a) * speed,
        vy: Math.sin(a) * speed,
        damage: 12,
        color: THEME.bullet.enemy,
        isPlayer: false,
        radius: 5
      });
    }
  };
  const fireBossAimed = (boss, p, g, count, speed, spread) => {
    const baseA = Math.atan2(p.y - boss.y, p.x - boss.x);
    for (let i = 0; i < count; i++) {
      const a = baseA + (i - (count - 1) / 2) * spread;
      g.bullets.push({
        x: boss.x,
        y: boss.y,
        vx: Math.cos(a) * speed,
        vy: Math.sin(a) * speed,
        damage: 15,
        color: '#fca5a5',
        isPlayer: false,
        radius: 5
      });
    }
  };
  const updateBossAI = (boss, g) => {
    const p = g.player;
    boss.frame++;
    if (boss.phase === 1 && boss.hp <= boss.maxHp * 0.5) {
      boss.phase = 2;
      g.shake = 25;
      spawnExplosion(boss.x, boss.y, boss.color, 3);
      spawnText(boss.x, boss.y - 70, "⚠️ PHASE 2 ⚠️", "#ff4444");
      boss.iframes = 60;
    }
    if (boss.y < (boss.targetY || 100)) {
      boss.y += 1.5;
      return;
    }
    if (boss.iframes > 0) {
      boss.iframes--;
      return;
    }
    const bt = boss.bossType;
    if (bt !== 'iron_clad' || !boss.ai.charging) {
      boss.x += Math.sin(boss.frame * 0.015) * (bt === 'god_killer' ? 3 : 1.5);
      boss.x = clamp(boss.x, 60, g.width - 60);
    }
    if (bt === 'gatekeeper') {
      const interval = boss.phase === 2 ? 35 : 55;
      if (boss.frame % interval === 0) fireBossSpread(boss, g, boss.phase === 2 ? 8 : 5, 3.5);
      if (boss.phase === 2 && boss.frame % 80 === 0) fireBossAimed(boss, p, g, 3, 5, 0.2);
    } else if (bt === 'hive_mother') {
      const si = boss.phase === 2 ? 70 : 110;
      const mc = boss.phase === 2 ? 4 : 2;
      if (boss.frame % si === 0) {
        for (let i = 0; i < mc; i++) g.enemies.push(createEnemy('minion', DIFF_MULT[g.levelIdx], g.width, boss.x + rand(-40, 40), boss.y + 30));
      }
      if (boss.phase === 2 && boss.frame % 55 === 0) fireBossAimed(boss, p, g, 1, 4, 0);
    } else if (bt === 'iron_clad') {
      if (!boss.ai.charging) {
        boss.ai.chargeCD = (boss.ai.chargeCD || 0) - 1;
        if (boss.ai.chargeCD <= 0) {
          boss.ai.charging = true;
          boss.ai.chargeDir = p.x > boss.x ? 1 : -1;
          boss.ai.chargeTick = 0;
          spawnText(boss.x, boss.y - 40, "CHARGE!", "#fdba74");
        }
        if (boss.frame % 60 === 0) fireBossAimed(boss, p, g, boss.phase === 2 ? 3 : 1, 4, 0.15);
      } else {
        boss.ai.chargeTick++;
        if (boss.ai.chargeTick < 20) {
          boss.x += (Math.random() - 0.5) * 4;
        } else if (boss.ai.chargeTick < 55) {
          boss.x += boss.ai.chargeDir * 12;
          if (boss.phase === 2 && boss.ai.chargeTick % 4 === 0) g.bullets.push({
            x: boss.x,
            y: boss.y,
            vx: 0,
            vy: 4,
            damage: 10,
            color: '#fdba74',
            isPlayer: false,
            radius: 5
          });
        } else {
          boss.ai.charging = false;
          boss.ai.chargeCD = boss.phase === 2 ? 90 : 150;
          boss.x = clamp(boss.x, 60, g.width - 60);
        }
      }
    } else if (bt === 'corruptor') {
      boss.ai.laserAng = (boss.ai.laserAng || 0) + (boss.phase === 2 ? 0.018 : 0.012);
      const lx = boss.x + Math.cos(boss.ai.laserAng) * 900;
      const ly = boss.y + Math.sin(boss.ai.laserAng) * 900;
      if (p.iframes <= 0 && g.frame % 8 === 0) {
        const d = pointToSegDist(p.x, p.y, boss.x, boss.y, lx, ly);
        if (d < p.radius + 8) {
          let dmg = 12 * (1 - p.stats.defense);
          if (p.shield > 0) {
            p.shield -= dmg;
            if (p.shield < 0) {
              p.hp += p.shield;
              p.shield = 0;
            }
          } else p.hp -= dmg;
          p.iframes = 15;
          g.shake = 5;
          spawnText(p.x, p.y, `-${Math.ceil(dmg)}`, '#ef4444');
        }
      }
      if (boss.phase === 2) {
        boss.ai.laserAng2 = (boss.ai.laserAng2 || Math.PI) + 0.015;
        const lx2 = boss.x + Math.cos(boss.ai.laserAng2) * 900;
        const ly2 = boss.y + Math.sin(boss.ai.laserAng2) * 900;
        if (p.iframes <= 0 && g.frame % 8 === 0) {
          const d2 = pointToSegDist(p.x, p.y, boss.x, boss.y, lx2, ly2);
          if (d2 < p.radius + 8) {
            let dmg = 12 * (1 - p.stats.defense);
            if (p.shield > 0) {
              p.shield -= dmg;
              if (p.shield < 0) {
                p.hp += p.shield;
                p.shield = 0;
              }
            } else p.hp -= dmg;
            p.iframes = 15;
            g.shake = 5;
            spawnText(p.x, p.y, `-${Math.ceil(dmg)}`, '#ef4444');
          }
        }
        if (boss.frame % 90 === 0) {
          const ha = Math.atan2(p.y - boss.y, p.x - boss.x);
          g.bullets.push({
            x: boss.x,
            y: boss.y,
            vx: Math.cos(ha) * 2.5,
            vy: Math.sin(ha) * 2.5,
            damage: 18,
            color: THEME.bullet.homing,
            isPlayer: false,
            radius: 6,
            homing: true
          });
        }
      }
    } else if (bt === 'omega_zero') {
      const spiralSpeed = boss.phase === 2 ? 0.12 : 0.08;
      boss.ai.spiralAng = (boss.ai.spiralAng || 0) + spiralSpeed;
      if (boss.frame % 5 === 0) {
        const a = boss.ai.spiralAng;
        g.bullets.push({
          x: boss.x,
          y: boss.y,
          vx: Math.cos(a) * 4,
          vy: Math.sin(a) * 4,
          damage: 10,
          color: '#f43f5e',
          isPlayer: false,
          radius: 4
        });
        if (boss.phase === 2) g.bullets.push({
          x: boss.x,
          y: boss.y,
          vx: Math.cos(a + Math.PI) * 4,
          vy: Math.sin(a + Math.PI) * 4,
          damage: 10,
          color: '#f43f5e',
          isPlayer: false,
          radius: 4
        });
      }
      if (boss.frame % 80 === 0) fireBossAimed(boss, p, g, boss.phase === 2 ? 5 : 3, 5, 0.15);
    } else if (bt === 'god_killer') {
      boss.ai.patternTimer = (boss.ai.patternTimer || 0) - 1;
      if (boss.ai.patternTimer <= 0) {
        boss.ai.pattern = Math.floor(Math.random() * 3);
        boss.ai.patternTimer = boss.phase === 2 ? 60 : 100;
      }
      const spd = boss.phase === 2 ? 1.5 : 1;
      if (boss.ai.pattern === 0 && boss.frame % Math.floor(40 / spd) === 0) fireBossSpread(boss, g, boss.phase === 2 ? 12 : 8, 4);
      if (boss.ai.pattern === 1) {
        boss.ai.spiralAng = (boss.ai.spiralAng || 0) + 0.1 * spd;
        if (boss.frame % 4 === 0) g.bullets.push({
          x: boss.x,
          y: boss.y,
          vx: Math.cos(boss.ai.spiralAng) * 4.5,
          vy: Math.sin(boss.ai.spiralAng) * 4.5,
          damage: 12,
          color: '#ef4444',
          isPlayer: false,
          radius: 4
        });
      }
      if (boss.ai.pattern === 2 && boss.frame % Math.floor(50 / spd) === 0) fireBossAimed(boss, p, g, boss.phase === 2 ? 7 : 5, 5.5, 0.12);
      if (boss.phase === 2 && boss.frame % 120 === 0) {
        for (let i = 0; i < 3; i++) g.enemies.push(createEnemy('minion', DIFF_MULT[g.levelIdx], g.width, boss.x + rand(-50, 50), boss.y + 40));
      }
    }
  };
  const update = () => {
    const g = engine.current;
    if (!g.active) return;
    g.frame++;
    const p = g.player;
    const config = LEVEL_CONFIG[g.levelIdx];
    const synOff = p.blessingCounts.offense >= 3 ? 0.15 : 0;
    const synDef = p.blessingCounts.defense >= 3 ? 0.05 : 0;
    const synXp = p.blessingCounts.utility >= 3 ? 0.25 : 0;
    if (p.stats.frenzyTimer > 0) p.stats.frenzyTimer--;
    if (p.stats.xpBoostTimer > 0) p.stats.xpBoostTimer--;
    if (p.stats.regen > 0) {
      p.stats.regenTimer = (p.stats.regenTimer || 0) + 1;
      if (p.stats.regenTimer >= 180) {
        p.hp = Math.min(p.maxHp, p.hp + p.stats.regen);
        p.stats.regenTimer = 0;
      }
    }
    if (p.stats.shieldRegen > 0) {
      p.stats.shieldRegenTimer = (p.stats.shieldRegenTimer || 0) + 1;
      if (p.stats.shieldRegenTimer >= 600) {
        p.shield += p.stats.shieldRegen;
        p.stats.shieldRegenTimer = 0;
        spawnText(p.x, p.y, '+SHIELD', '#60a5fa');
      }
    }
    g.playerTrail.push({
      x: p.x,
      y: p.y
    });
    if (g.playerTrail.length > 8) g.playerTrail.shift();
    const dx = g.mouse.x - p.x,
      dy = g.mouse.y - p.y;
    p.vx += dx * 0.08 * p.stats.speed;
    p.vy += dy * 0.08 * p.stats.speed;
    p.vx *= p.stats.friction;
    p.vy *= p.stats.friction;
    p.x += p.vx;
    p.y += p.vy;
    p.x = clamp(p.x, 10, g.width - 10);
    p.y = clamp(p.y, 10, g.height - 10);
    if (p.stats.fireTimer > 0) p.stats.fireTimer--;else {
      const rate = p.stats.frenzyTimer > 0 ? Math.max(2, Math.floor(p.stats.fireRate / 2)) : p.stats.fireRate;
      p.stats.fireTimer = rate;
      const baseDmg = p.stats.damage * (1 + synOff);
      for (let i = 0; i < p.stats.bulletCount; i++) {
        let angle = -Math.PI / 2;
        if (p.stats.bulletCount > 1) angle += (i - (p.stats.bulletCount - 1) / 2) * p.stats.spread;
        const isCrit = Math.random() < p.stats.critChance;
        const dmg = isCrit ? baseDmg * 2 : baseDmg;
        const spd = 15 * p.stats.bulletSpeed;
        g.bullets.push({
          x: p.x,
          y: p.y - 15,
          vx: Math.cos(angle) * 2 * p.stats.bulletSpeed,
          vy: Math.sin(angle) * spd,
          damage: dmg,
          isCrit,
          pierce: p.stats.pierce,
          plasma: p.stats.plasma,
          color: p.stats.plasma ? THEME.bullet.plasma : isCrit ? THEME.bullet.crit : THEME.bullet.normal,
          isPlayer: true
        });
      }
    }
    if (!g.bossActive && !g.transitioning) {
      g.levelTimer++;
      if (g.levelTimer > config.duration * 60) {
        g.bossActive = true;
        const bd = config.boss;
        const boss = {
          x: g.width / 2,
          y: -100,
          vx: 0,
          vy: 0,
          hp: bd.hp,
          maxHp: bd.hp,
          radius: 55,
          type: 'boss',
          name: bd.name,
          color: bd.color,
          isBoss: true,
          bossType: bd.bossType,
          phase: 1,
          frame: 0,
          iframes: 0,
          targetY: bd.bossType === 'corruptor' ? 80 : 100,
          ai: {}
        };
        g.enemies.push(boss);
        setUiData(u => ({
          ...u,
          showBossBar: true,
          bossHp: bd.hp,
          maxBossHp: bd.hp,
          bossName: bd.name
        }));
        spawnText(g.width / 2, 200, "⚠️ " + bd.name + " ⚠️", "#ef4444");
        g.shake = 20;
      } else {
        if (g.spawnTimer > 0) g.spawnTimer--;else {
          g.spawnTimer = config.spawnRate;
          const type = config.types[Math.floor(Math.random() * config.types.length)];
          const diffMod = DIFF_MULT[g.levelIdx] * (1 + g.levelTimer / 3600);
          g.enemies.push(createEnemy(type, diffMod, g.width));
        }
      }
    }
    for (let i = g.bullets.length - 1; i >= 0; i--) {
      const b = g.bullets[i];
      if (b.homing) {
        const ha = Math.atan2(p.y - b.y, p.x - b.x);
        b.vx += Math.cos(ha) * 0.12;
        b.vy += Math.sin(ha) * 0.12;
        const spd = Math.sqrt(b.vx * b.vx + b.vy * b.vy);
        if (spd > 4) {
          b.vx = b.vx / spd * 4;
          b.vy = b.vy / spd * 4;
        }
      }
      b.x += b.vx;
      b.y += b.vy;
      if (b.x < -50 || b.x > g.width + 50 || b.y < -50 || b.y > g.height + 50) g.bullets.splice(i, 1);
    }
    for (let i = g.enemies.length - 1; i >= 0; i--) {
      const e = g.enemies[i];
      if (!e) continue;
      let destroyed = false;
      if (e.isBoss) {
        updateBossAI(e, g);
      } else {
        e.frame++;
        if (e.type === 'drone') {
          e.x += Math.sin(e.frame * 0.05 + (e.ai.wobble || 0)) * 0.8;
          e.y += e.vy;
        } else if (e.type === 'sprinter') {
          const tx = p.x - e.x;
          e.vx += tx * 0.003;
          e.vx *= 0.95;
          e.x += e.vx;
          e.y += e.vy;
        } else if (e.type === 'sentinel') {
          e.y += e.vy;
          e.ai.shieldAng += 0.03;
        } else if (e.type === 'sniper') {
          if (!e.ai.stopped && e.y > 120) {
            e.ai.stopped = true;
            e.vy = 0;
          }
          if (e.ai.stopped) {
            e.ai.shootTimer--;
            if (e.ai.shootTimer <= 0) {
              e.ai.shootTimer = 80;
              const sa = Math.atan2(p.y - e.y, p.x - e.x);
              g.bullets.push({
                x: e.x,
                y: e.y,
                vx: Math.cos(sa) * 6,
                vy: Math.sin(sa) * 6,
                damage: 15,
                color: '#c7d2fe',
                isPlayer: false,
                radius: 4
              });
            }
          } else e.y += e.vy;
        } else if (e.type === 'warper') {
          e.y += e.vy;
          e.ai.teleTimer--;
          if (e.ai.teleTimer <= 0) {
            e.ai.teleTimer = 150;
            e.x = rand(30, g.width - 30);
            e.y = rand(50, g.height * 0.5);
            spawnExplosion(e.x, e.y, e.color, 0.5);
          }
          e.ai.burstTimer--;
          if (e.ai.burstTimer <= 0) {
            e.ai.burstTimer = 90;
            for (let k = 0; k < 4; k++) {
              const ba = Math.PI * 2 / 4 * k;
              g.bullets.push({
                x: e.x,
                y: e.y,
                vx: Math.cos(ba) * 3,
                vy: Math.sin(ba) * 3,
                damage: 10,
                color: '#fde047',
                isPlayer: false,
                radius: 4
              });
            }
          }
        } else {
          e.x += e.vx;
          e.y += e.vy;
        }
      }
      if (!destroyed) {
        for (let j = g.bullets.length - 1; j >= 0; j--) {
          const b = g.bullets[j];
          if (!b || !b.isPlayer) continue;
          const hitRadius = b.plasma ? 28 : 6;
          if (checkCircleCollide({
            x: b.x,
            y: b.y,
            radius: hitRadius
          }, e)) {
            if (e.isBoss && e.iframes > 0) continue;
            if (e.type === 'sentinel') {
              const angToB = Math.atan2(b.y - e.y, b.x - e.x);
              const diff = Math.abs(angToB - e.ai.shieldAng) % (Math.PI * 2);
              if (diff < 0.8 || diff > Math.PI * 2 - 0.8) {
                g.bullets.splice(j, 1);
                spawnText(e.x, e.y, "BLOCKED", "#fb923c");
                continue;
              }
            }
            e.hp -= b.damage;
            if (b.isCrit) spawnText(e.x, e.y - 10, Math.floor(b.damage) + "!", "#facc15");
            if (b.plasma) spawnExplosion(b.x, b.y, THEME.bullet.plasma, 0.6);else spawnExplosion(b.x, b.y, b.color, 0.3);
            if (b.pierce > 0) b.pierce--;else if (!b.plasma) g.bullets.splice(j, 1);
            if (e.hp <= 0) {
              if (e.isBoss) {
                spawnExplosion(e.x, e.y, e.color, 5);
                spawnText(e.x, e.y, e.name + " DEFEATED", "#fff");
                g.bossActive = false;
                g.bossDefeated++;
                g.transitioning = true;
                p.hp = p.maxHp;
                p.shield += 30;
                for (let k = 0; k < 20; k++) spawnLoot(e.x + rand(-80, 80), e.y + rand(-80, 80));
                g.shake = 30;
                p.score += 2000;
                if (g.levelIdx >= LEVEL_CONFIG.length - 1) {
                  setTimeout(() => {
                    setGameState('VICTORY');
                    g.active = false;
                  }, 1500);
                } else {
                  setTimeout(() => {
                    g.levelIdx++;
                    g.levelTimer = 0;
                    g.enemies = [];
                    g.bullets = [];
                    g.loots = [];
                    g.exps = [];
                    g.transitioning = false;
                    playStory(g.levelIdx);
                  }, 2500);
                }
              } else {
                spawnExplosion(e.x, e.y, e.color, 0.8);
                const boostMult = p.stats.xpBoostTimer > 0 ? 2 : 1;
                const xpVal = Math.floor((e.exp || 10) * p.stats.xpMult * (1 + synXp) * boostMult);
                p.exp += xpVal;
                spawnText(e.x, e.y, `+${xpVal} XP`, '#facc15');
                if (p.exp >= p.expNext) {
                  p.exp -= p.expNext;
                  p.level++;
                  p.expNext = Math.floor(p.expNext * 1.25);
                  setTimeout(triggerLevelUp, 10);
                }
                if (Math.random() < 0.45) spawnLoot(e.x, e.y);
                if (p.stats.lifesteal > 0 && Math.random() < p.stats.lifesteal) {
                  p.hp = Math.min(p.maxHp, p.hp + 3);
                  spawnText(p.x, p.y, "+3 HP", "#22c55e");
                }
                p.score += 100;
              }
              setUiData(u => ({
                ...u,
                score: p.score
              }));
              g.enemies.splice(i, 1);
              destroyed = true;
              break;
            }
          }
        }
      }
      if (!destroyed && p.iframes <= 0 && checkCircleCollide(p, e)) {
        if (Math.random() < p.stats.dodge + synDef) {
          spawnText(p.x, p.y, "DODGE!", "#a5f3fc");
        } else {
          let dmg = (e.isBoss ? 25 : 20) * (1 - p.stats.defense - synDef);
          dmg = Math.max(1, dmg);
          if (p.shield > 0) {
            p.shield -= dmg;
            if (p.shield < 0) {
              p.hp += p.shield;
              p.shield = 0;
            }
          } else p.hp -= dmg;
          if (p.stats.thorns > 0) {
            const td = dmg * p.stats.thorns;
            e.hp -= td;
          }
          p.iframes = 60;
          g.shake = 10;
          spawnText(p.x, p.y, `-${Math.ceil(dmg)}`, "#ef4444");
        }
        if (!e.isBoss) {
          g.enemies.splice(i, 1);
          destroyed = true;
          spawnExplosion(e.x, e.y, e.color);
        }
      }
      if (!destroyed && !e.isBoss && e.y > g.height + 50) g.enemies.splice(i, 1);
    }
    for (let i = g.bullets.length - 1; i >= 0; i--) {
      const b = g.bullets[i];
      if (b.isPlayer) continue;
      if (p.iframes <= 0 && checkCircleCollide(p, {
        x: b.x,
        y: b.y,
        radius: b.radius || 5
      })) {
        if (Math.random() < p.stats.dodge + synDef) {
          spawnText(p.x, p.y, "DODGE!", "#a5f3fc");
          g.bullets.splice(i, 1);
          continue;
        }
        let dmg = (b.damage || 10) * (1 - p.stats.defense - synDef);
        dmg = Math.max(1, dmg);
        if (p.shield > 0) {
          p.shield -= dmg;
          if (p.shield < 0) {
            p.hp += p.shield;
            p.shield = 0;
          }
        } else p.hp -= dmg;
        p.iframes = 30;
        g.shake = 5;
        spawnText(p.x, p.y, `-${Math.ceil(dmg)}`, "#ef4444");
        g.bullets.splice(i, 1);
      }
    }
    for (let i = g.loots.length - 1; i >= 0; i--) {
      const l = g.loots[i];
      l.y += 1;
      if (dist(l.x, l.y, p.x, p.y) < p.radius + 20) {
        if (l.type === 'hp') {
          p.hp = Math.min(p.maxHp, p.hp + 20);
          spawnText(p.x, p.y, "REPAIR", THEME.loot.hp);
        } else if (l.type === 'shield') {
          p.shield += 25;
          spawnText(p.x, p.y, "SHIELD UP", THEME.loot.shield);
        } else if (l.type === 'power') {
          p.stats.xpBoostTimer = 600;
          spawnText(p.x, p.y, t('XP BOOST!', '经验加速!'), THEME.loot.power);
        } else if (l.type === 'weapon') {
          p.stats.damage += 1;
          spawnText(p.x, p.y, "DMG UP", THEME.loot.weapon);
        } else if (l.type === 'frenzy') {
          p.stats.frenzyTimer = 300;
          spawnText(p.x, p.y, "FRENZY!", THEME.loot.frenzy);
        } else if (l.type === 'nuke') {
          for (let ni = g.enemies.length - 1; ni >= 0; ni--) {
            const en = g.enemies[ni];
            if (!en.isBoss) {
              spawnExplosion(en.x, en.y, en.color, 2);
              g.enemies.splice(ni, 1);
            } else {
              en.hp -= 800;
              spawnExplosion(en.x, en.y, en.color, 1.5);
            }
          }
          g.shake = 30;
          spawnText(p.x, p.y, "NUKE!", THEME.loot.nuke);
        }
        g.loots.splice(i, 1);
      } else if (l.y > g.height) g.loots.splice(i, 1);
    }
    if (p.hp <= 0) {
      g.active = false;
      setGameState('GAMEOVER');
      onGameOver(p.score);
    }
    if (p.iframes > 0) p.iframes--;
    g.shake *= 0.9;
    for (let i = g.particles.length - 1; i >= 0; i--) {
      const pt = g.particles[i];
      pt.life--;
      pt.x += pt.vx;
      pt.y += pt.vy;
      pt.vx *= 0.95;
      pt.vy *= 0.95;
      if (pt.life <= 0) g.particles.splice(i, 1);
    }
    for (let i = g.floatTexts.length - 1; i >= 0; i--) {
      g.floatTexts[i].life--;
      g.floatTexts[i].y += g.floatTexts[i].vy;
      if (g.floatTexts[i].life <= 0) g.floatTexts.splice(i, 1);
    }
    if (g.frame % 5 === 0) {
      let bHp = 0,
        bMax = 0,
        bName = '';
      const boss = g.enemies.find(e => e.isBoss);
      if (boss) {
        bHp = boss.hp;
        bMax = boss.maxHp;
        bName = boss.name;
      }
      setUiData(u => ({
        ...u,
        hp: p.hp,
        maxHp: p.maxHp,
        shield: p.shield,
        score: p.score,
        level: p.level,
        exp: p.exp,
        expNext: p.expNext,
        bossHp: bHp,
        maxBossHp: bMax,
        showBossBar: !!boss,
        bossName: bName,
        playerStats: {
          ...p.stats,
          hp: p.hp,
          maxHp: p.maxHp,
          shield: p.shield,
          blessingCounts: {
            ...p.blessingCounts
          }
        }
      }));
    }
  };
  const draw = ctx => {
    const g = engine.current;
    const {
      width,
      height
    } = g;
    const p = g.player;
    ctx.fillStyle = '#0a0e1a';
    ctx.fillRect(0, 0, width, height);
    ctx.save();
    if (g.shake > 0.5) ctx.translate((Math.random() - 0.5) * g.shake, (Math.random() - 0.5) * g.shake);
    if (g.stars.length === 0) {
      for (let i = 0; i < 120; i++) g.stars.push({
        x: rand(0, width),
        y: rand(0, height),
        size: rand(0.5, 2.5),
        speed: rand(0.3, 1.2),
        brightness: rand(0.2, 0.9)
      });
    }
    g.stars.forEach(s => {
      s.y += s.speed;
      if (s.y > height) {
        s.y = 0;
        s.x = rand(0, width);
      }
      const twinkle = s.brightness * (0.7 + Math.sin(g.frame * 0.02 + s.x) * 0.3);
      ctx.fillStyle = `rgba(148,163,184,${twinkle})`;
      ctx.fillRect(s.x, s.y, s.size, s.size);
    });
    ctx.strokeStyle = THEME.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    const off = g.frame % 40;
    for (let x = 0; x <= width; x += 40) {
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
    }
    for (let y = off; y <= height; y += 40) {
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
    }
    ctx.stroke();
    g.loots.forEach(l => {
      ctx.shadowBlur = 10;
      ctx.shadowColor = l.color;
      ctx.fillStyle = l.color;
      ctx.beginPath();
      if (l.type === 'hp') {
        ctx.moveTo(l.x, l.y - 5);
        ctx.lineTo(l.x + 5, l.y);
        ctx.lineTo(l.x, l.y + 5);
        ctx.lineTo(l.x - 5, l.y);
      } else if (l.type === 'shield') ctx.arc(l.x, l.y, 6, 0, Math.PI * 2);else ctx.rect(l.x - 6, l.y - 6, 12, 12);
      ctx.fill();
      ctx.fillStyle = '#000';
      ctx.font = '10px sans-serif';
      ctx.textAlign = 'center';
      let icon = '⚡';
      if (l.type === 'hp') icon = '✚';
      if (l.type === 'shield') icon = '🛡️';
      if (l.type === 'weapon') icon = '⚔️';
      if (l.type === 'nuke') icon = '☢️';
      if (l.type === 'frenzy') icon = '🔥';
      ctx.fillText(icon, l.x, l.y + 4);
    });
    ctx.shadowBlur = 0;
    g.exps.forEach(xp => {
      ctx.fillStyle = '#facc15';
      ctx.globalAlpha = 0.8;
      ctx.beginPath();
      ctx.arc(xp.x, xp.y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    });
    g.bullets.forEach(b => {
      ctx.shadowBlur = b.isPlayer ? 8 : 5;
      ctx.shadowColor = b.color;
      ctx.fillStyle = b.color;
      if (b.plasma) {
        ctx.globalAlpha = 0.3;
        ctx.beginPath();
        ctx.arc(b.x, b.y, 12, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.beginPath();
        ctx.arc(b.x, b.y, 5, 0, Math.PI * 2);
        ctx.fill();
      } else if (b.homing) {
        ctx.globalAlpha = 0.3;
        ctx.beginPath();
        ctx.arc(b.x, b.y, (b.radius || 5) + 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.beginPath();
        ctx.arc(b.x, b.y, b.radius || 5, 0, Math.PI * 2);
        ctx.fill();
      } else if (b.isPlayer && b.isCrit) {
        ctx.fillRect(b.x - 3, b.y - 6, 6, 12);
        ctx.globalAlpha = 0.4;
        ctx.fillRect(b.x - 5, b.y - 2, 10, 4);
        ctx.globalAlpha = 1;
      } else {
        ctx.fillRect(b.x - 2, b.y - 4, 4, 8);
      }
    });
    ctx.shadowBlur = 0;
    g.enemies.forEach(e => {
      ctx.save();
      ctx.translate(e.x, e.y);
      ctx.shadowBlur = 10;
      ctx.shadowColor = e.color;
      ctx.fillStyle = e.color;
      if (e.isBoss) {
        ctx.save();
        ctx.globalAlpha = 0.15 + Math.sin(g.frame * 0.05) * 0.08;
        ctx.strokeStyle = e.color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(0, 0, e.radius + 20 + Math.sin(g.frame * 0.08) * 5, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
        ctx.save();
        ctx.rotate(g.frame * 0.015);
        const r = e.radius;
        if (e.bossType === 'gatekeeper') {
          ctx.beginPath();
          for (let i = 0; i < 5; i++) {
            const a = Math.PI * 2 / 5 * i - Math.PI / 2;
            ctx.lineTo(Math.cos(a) * r, Math.sin(a) * r);
          }
          ctx.closePath();
          ctx.fill();
        } else if (e.bossType === 'hive_mother') {
          ctx.beginPath();
          ctx.arc(0, 0, r, 0, Math.PI * 2);
          ctx.fill();
          for (let i = 0; i < 5; i++) {
            const a = g.frame * 0.03 + Math.PI * 2 / 5 * i;
            ctx.beginPath();
            ctx.arc(Math.cos(a) * r * 0.7, Math.sin(a) * r * 0.7, 8, 0, Math.PI * 2);
            ctx.fill();
          }
        } else if (e.bossType === 'iron_clad') {
          ctx.fillRect(-r, -r * 0.7, r * 2, r * 1.4);
          ctx.fillStyle = '#fff';
          ctx.fillRect(-8, -8, 16, 16);
        } else if (e.bossType === 'corruptor') {
          if (g.frame % 6 < 3) ctx.translate(rand(-3, 3), rand(-3, 3));
          ctx.beginPath();
          for (let i = 0; i < 6; i++) {
            const a = Math.PI * 2 / 6 * i;
            ctx.lineTo(Math.cos(a) * r, Math.sin(a) * r);
          }
          ctx.closePath();
          ctx.fill();
        } else if (e.bossType === 'omega_zero') {
          ctx.beginPath();
          for (let i = 0; i < 8; i++) {
            const a = Math.PI * 2 / 8 * i;
            const rr = i % 2 === 0 ? r : r * 0.5;
            ctx.lineTo(Math.cos(a) * rr, Math.sin(a) * rr);
          }
          ctx.closePath();
          ctx.fill();
        } else {
          ctx.beginPath();
          for (let i = 0; i < 6; i++) {
            const a = Math.PI * 2 / 6 * i;
            ctx.lineTo(Math.cos(a) * r, Math.sin(a) * r);
          }
          ctx.closePath();
          ctx.fill();
          ctx.fillStyle = '#fff';
          ctx.beginPath();
          ctx.arc(0, 0, r * 0.3, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.restore();
        ctx.beginPath();
        ctx.arc(0, 0, e.radius + 5, 0, Math.PI * 2);
        ctx.lineWidth = 2;
        ctx.strokeStyle = e.color;
        ctx.stroke();
        ctx.fillStyle = '#fff';
        ctx.fillRect(-10, -10, 20, 20);
        if (e.bossType === 'corruptor') {
          ctx.save();
          ctx.rotate(e.ai.laserAng || 0);
          ctx.fillStyle = `rgba(129,140,248,${0.4 + Math.sin(g.frame * 0.3) * 0.2})`;
          ctx.shadowBlur = 15;
          ctx.shadowColor = '#818cf8';
          ctx.fillRect(0, -4, 900, 8);
          ctx.restore();
          if (e.phase === 2) {
            ctx.save();
            ctx.rotate(e.ai.laserAng2 || Math.PI);
            ctx.fillStyle = `rgba(129,140,248,${0.3 + Math.sin(g.frame * 0.25) * 0.15})`;
            ctx.fillRect(0, -3, 900, 6);
            ctx.restore();
          }
        }
      } else {
        ctx.save();
        ctx.rotate(g.frame * 0.08);
        if (e.type === 'sentinel') {
          ctx.beginPath();
          for (let i = 0; i < 6; i++) {
            const a = Math.PI * 2 / 6 * i;
            ctx.lineTo(Math.cos(a) * e.radius, Math.sin(a) * e.radius);
          }
          ctx.closePath();
          ctx.fill();
          ctx.restore();
          ctx.save();
          ctx.rotate(e.ai.shieldAng || 0);
          ctx.strokeStyle = '#fdba74';
          ctx.lineWidth = 3;
          ctx.beginPath();
          ctx.arc(0, 0, e.radius + 4, -0.6, 0.6);
          ctx.stroke();
        } else if (e.type === 'sprinter') {
          ctx.beginPath();
          ctx.moveTo(0, -10);
          ctx.lineTo(8, 8);
          ctx.lineTo(-8, 8);
          ctx.closePath();
          ctx.fill();
        } else if (e.type === 'sniper') {
          ctx.fillRect(-8, -8, 16, 16);
          ctx.restore();
          ctx.save();
          ctx.strokeStyle = e.color;
          ctx.lineWidth = 1;
          ctx.globalAlpha = 0.4;
          ctx.beginPath();
          ctx.moveTo(-12, 0);
          ctx.lineTo(12, 0);
          ctx.moveTo(0, -12);
          ctx.lineTo(0, 12);
          ctx.stroke();
        } else if (e.type === 'warper') {
          if (g.frame % 8 < 4) ctx.globalAlpha = 0.5;
          ctx.beginPath();
          ctx.moveTo(0, -12);
          ctx.lineTo(12, 0);
          ctx.lineTo(0, 12);
          ctx.lineTo(-12, 0);
          ctx.closePath();
          ctx.fill();
        } else if (e.type === 'minion') {
          ctx.beginPath();
          ctx.arc(0, 0, e.radius, 0, Math.PI * 2);
          ctx.fill();
        } else {
          ctx.fillRect(-8, -8, 16, 16);
        }
        ctx.restore();
        if (e.hp < e.maxHp && e.maxHp > 30) {
          ctx.save();
          ctx.globalAlpha = 0.7;
          const bw = e.radius * 2;
          ctx.fillStyle = '#1e293b';
          ctx.fillRect(-bw / 2, -e.radius - 8, bw, 3);
          ctx.fillStyle = e.color;
          ctx.fillRect(-bw / 2, -e.radius - 8, bw * (e.hp / e.maxHp), 3);
          ctx.restore();
        }
      }
      ctx.restore();
    });
    g.playerTrail.forEach((pos, i) => {
      ctx.globalAlpha = i / g.playerTrail.length * 0.25;
      ctx.fillStyle = THEME.player;
      ctx.beginPath();
      ctx.arc(pos.x, pos.y + 5, 3, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.globalAlpha = 1;
    if (g.active) {
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.vx * 0.08);
      if (p.iframes > 0 && Math.floor(g.frame / 4) % 2 === 0) ctx.globalAlpha = 0.5;
      ctx.shadowBlur = 15;
      ctx.shadowColor = THEME.player;
      ctx.fillStyle = THEME.player;
      ctx.beginPath();
      ctx.moveTo(0, -15);
      ctx.lineTo(12, 10);
      ctx.lineTo(0, 5);
      ctx.lineTo(-12, 10);
      ctx.fill();
      ctx.fillStyle = '#f59e0b';
      ctx.globalAlpha = 0.6 + Math.random() * 0.4;
      ctx.beginPath();
      ctx.moveTo(-4, 10);
      ctx.lineTo(0, 10 + 6 + Math.random() * 6);
      ctx.lineTo(4, 10);
      ctx.fill();
      ctx.globalAlpha = 1;
      if (p.shield > 0) {
        ctx.strokeStyle = '#60a5fa';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(0, 0, 20 + Math.sin(g.frame * 0.2) * 2, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();
    }
    ctx.globalCompositeOperation = 'lighter';
    g.particles.forEach(pt => {
      ctx.fillStyle = pt.color;
      ctx.globalAlpha = pt.life / pt.maxLife;
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, pt.size, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.font = 'bold 12px "JetBrains Mono"';
    ctx.textAlign = 'center';
    g.floatTexts.forEach(t => {
      ctx.fillStyle = t.color;
      ctx.globalAlpha = t.life / 50;
      ctx.fillText(t.text, t.x, t.y);
    });
    ctx.globalAlpha = 1;
    if (g.active && p.hp / p.maxHp < 0.3) {
      ctx.save();
      const grad = ctx.createRadialGradient(width / 2, height / 2, height * 0.3, width / 2, height / 2, height * 0.7);
      grad.addColorStop(0, 'transparent');
      grad.addColorStop(1, `rgba(239,68,68,${0.12 + Math.sin(g.frame * 0.1) * 0.08})`);
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, width, height);
      ctx.restore();
    }
    ctx.save();
    ctx.globalAlpha = 0.03;
    for (let sy = 0; sy < height; sy += 3) {
      ctx.fillStyle = '#000';
      ctx.fillRect(0, sy, width, 1);
    }
    ctx.restore();
    if (g.active && p.stats.xpBoostTimer > 0) {
      ctx.save();
      ctx.globalAlpha = 0.08 + Math.sin(g.frame * 0.15) * 0.04;
      ctx.fillStyle = '#38bdf8';
      ctx.fillRect(0, 0, width, height);
      ctx.restore();
    }
    ctx.restore();
  };
  useEffect(() => {
    if (gameState === 'PLAYING') {
      engine.current.active = true;
    } else if (gameState === 'STORY' || gameState === 'INTRO' || gameState === 'BOOT') {
      engine.current.active = false;
    }
  }, [gameState]);
  useEffect(() => {
    const cvs = canvasRef.current;
    const ctx = cvs.getContext('2d');
    const move = e => {
      const r = cvs.getBoundingClientRect();
      engine.current.mouse = {
        x: (e.clientX - r.left) * (900 / r.width),
        y: (e.clientY - r.top) * (610 / r.height)
      };
    };
    window.addEventListener('mousemove', move);
    let raf;
    const loop = () => {
      try {
        update();
        draw(ctx);
      } catch (e) {
        console.error('[Game] RAF error:', e);
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('mousemove', move);
    };
  }, []);
  const rarityColor = r => r === 'legendary' ? 'text-yellow-400 border-yellow-500/40' : r === 'rare' ? 'text-purple-400 border-purple-500/40' : r === 'uncommon' ? 'text-green-400 border-green-500/40' : 'text-cyan-400 border-cyan-500/40';
  const catIcon = c => c === 'offense' ? '⚔️' : c === 'defense' ? '🛡️' : '⚡';
  return <div ref={containerRef} className={`relative w-full h-full bg-slate-950 overflow-visible ${gameState === 'PLAYING' ? 'cursor-none' : ''}`}>
            <canvas ref={canvasRef} width={900} height={610} className="block w-full h-full" />
            <StatsPanel stats={uiData.playerStats} />

            {}
            <div className="absolute top-0 left-0 w-full p-4 flex justify-between z-20 pointer-events-none">
                <div className="flex flex-col gap-1 w-64">
                    <div className="flex justify-between text-xs font-mono font-bold text-cyan-400">
                        <span>{t('HULL', '船体')}</span><span>{Math.ceil(uiData.hp)}/{uiData.maxHp}</span>
                    </div>
                    <div className="h-2 bg-slate-900/80 border border-slate-700 skew-x-[-10deg]">
                        <div className="h-full bg-gradient-to-r from-cyan-600 to-cyan-400 transition-all duration-200" style={{
            width: `${uiData.hp / uiData.maxHp * 100}%`
          }}></div>
                    </div>
                    {uiData.shield > 0 && <div className="h-1 mt-1 bg-slate-900/80 border border-blue-900/50 skew-x-[-10deg]">
                            <div className="h-full bg-blue-500 transition-all duration-200" style={{
            width: `${Math.min(100, uiData.shield / 50 * 100)}%`
          }}></div>
                        </div>}
                    <span className="text-[10px] text-slate-500 font-mono tracking-widest mt-1">{uiData.sectorName}</span>
                </div>
                <div className="text-right">
                    <div className="text-3xl font-black italic text-white tracking-tighter drop-shadow-lg">{uiData.score.toLocaleString()}</div>
                    <div className="text-xs text-yellow-400 font-mono">LVL {uiData.level}</div>
                </div>
            </div>

            {}
            <div className="absolute bottom-0 left-0 w-full h-1 bg-slate-900 z-20">
                <div className="h-full bg-yellow-400 shadow-[0_0_10px_rgba(250,204,21,0.8)] transition-all duration-300" style={{
        width: `${uiData.exp / uiData.expNext * 100}%`
      }}></div>
            </div>

            {}
            {uiData.showBossBar && <div className="absolute top-16 left-1/2 -translate-x-1/2 w-[500px] z-20 animate-in zoom-in duration-500">
                    <div className="text-center text-rose-500 font-black tracking-[0.3em] text-xs mb-1 animate-pulse">{uiData.bossName}</div>
                    <div className="h-4 bg-slate-900 border border-rose-900/50 rounded-full overflow-hidden shadow-rose-900/50 shadow-lg">
                        <div className="h-full bg-rose-600 transition-all duration-100" style={{
          width: `${uiData.bossHp / uiData.maxBossHp * 100}%`
        }}></div>
                    </div>
                </div>}

            {}
            {gameState === 'INTRO' && <div className="absolute inset-0 bg-black z-50 flex items-center justify-center">
                    <div className="max-w-2xl w-full p-8 space-y-6">
                        <div className="text-center mb-8">
                            <div className="text-xs text-cyan-500/60 font-mono tracking-[0.5em] mb-2 animate-pulse">██ {t('CLASSIFIED BRIEFING', '机密简报')} ██</div>
                            <h1 className="text-4xl font-black italic text-transparent bg-clip-text bg-gradient-to-r from-cyan-400 via-blue-500 to-purple-500">PROTOCOL OMEGA</h1>
                        </div>
                        <div className="border-l-2 border-cyan-500/40 pl-6 space-y-1">
                            {GAME_LORE[lang].map((line, i) => <div key={i} className={line === '' ? 'h-3' : 'text-sm font-mono text-slate-300 leading-relaxed'} style={{
            animationDelay: `${i * 100}ms`
          }}>{line}</div>)}
                        </div>
                        <div className="mt-6 pt-4 border-t border-slate-800">
                            <div className="text-xs font-bold text-cyan-400 font-mono mb-3 tracking-widest">▶ {t('FIELD DROPS', '战场掉落物')}</div>
                            <div className="grid grid-cols-2 gap-2">
                                {LOOT_GUIDE.map((item, i) => <div key={i} className="flex items-center gap-2 bg-slate-900/60 rounded-lg px-3 py-1.5 border border-slate-800/50">
                                        <span className="text-base" style={{
                textShadow: `0 0 8px ${item.color}`
              }}>{item.icon}</span>
                                        <span className="text-[11px] font-mono" style={{
                color: item.color
              }}>{item[lang]}</span>
                                    </div>)}
                            </div>
                        </div>
                        <div className="text-center mt-8">
                            <button onClick={startGame} className="px-8 py-3 bg-cyan-500/10 border border-cyan-500/50 text-cyan-400 font-bold font-mono rounded-xl tracking-widest hover:bg-cyan-500/20 hover:border-cyan-400 hover:shadow-[0_0_30px_rgba(34,211,238,0.3)] transition-all text-sm">
                                {t('▶ BEGIN OPERATION', '▶ 开始任务')}
                            </button>
                        </div>
                    </div>
                </div>}

            {}
            {gameState === 'STORY' && <div className="absolute inset-0 bg-black/95 z-50 flex items-center justify-center">
                    <div className="max-w-2xl p-8 border-l-2 border-cyan-500 bg-slate-900/50 backdrop-blur-sm">
                        <div className="text-3xl font-black text-cyan-500 italic mb-6">{uiData.sectorName}</div>
                        {uiData.storyLines.slice(0, uiData.storyIndex).map((line, i) => <div key={i} className="mb-4 animate-in fade-in slide-in-from-left-4 duration-500">
                                <div className="text-lg text-white font-mono">{lang === 'cn' ? line.cn : line.en}</div>
                                {lang !== 'cn' && <div className="text-sm text-slate-500">{line.cn}</div>}
                            </div>)}
                    </div>
                </div>}

            {}
            {gameState === 'LEVEL_UP' && <div className="absolute inset-0 bg-slate-950/85 backdrop-blur-lg z-50 flex items-center justify-center">
                    <div className="grid grid-cols-3 gap-4 max-w-4xl p-6">
                        <h2 className="col-span-3 text-center text-3xl font-black text-yellow-400 italic mb-4">{t('DATA EMPOWERMENT', '数据赋能')}</h2>
                        {upgradeChoices.map((u, i) => <button key={i} onClick={() => applyBlessing(u)} className={`group relative bg-slate-900 border p-6 rounded-xl text-left transition-all hover:-translate-y-1 hover:shadow-lg ${rarityColor(u.rarity)}`}>
                                <div className="flex items-center gap-2 mb-2">
                                    <span className="text-lg">{catIcon(u.cat)}</span>
                                    <span className={`text-xs font-bold uppercase ${rarityColor(u.rarity).split(' ')[0]}`}>[{u.rarity}]</span>
                                </div>
                                <div className="text-base font-bold text-white font-mono mb-1">{lang === 'cn' ? u.name.split('//')[1] : u.name.split('//')[0]}</div>
                                <div className="text-xs text-slate-500 mb-2 font-mono">{lang === 'cn' ? u.name.split('//')[0] : u.name.split('//')[1]}</div>
                                <div className="text-sm text-slate-300">{u.desc}</div>
                            </button>)}
                    </div>
                </div>}

            {}
            {gameState === 'GAMEOVER' && <div className="absolute inset-0 bg-black/90 z-50 flex flex-col items-center justify-center gap-6">
                    <div className="text-5xl font-black text-rose-500 italic tracking-tight">{t('NEURAL LINK SEVERED', '神经链接中断')}</div>
                    <div className="text-slate-400 font-mono text-sm">{t('System integrity: 0% — ARIA offline', '系统完整性: 0% — ARIA 已离线')}</div>
                    <div className="flex gap-8 mt-4">
                        <div className="text-center"><div className="text-2xl font-bold text-white">{uiData.score.toLocaleString()}</div><div className="text-[10px] text-slate-500 font-mono">{t('SCORE', '分数')}</div></div>
                        <div className="text-center"><div className="text-2xl font-bold text-yellow-400">{uiData.level}</div><div className="text-[10px] text-slate-500 font-mono">{t('LEVEL', '等级')}</div></div>
                        <div className="text-center"><div className="text-2xl font-bold text-cyan-400">SEC {uiData.sector}</div><div className="text-[10px] text-slate-500 font-mono">{t('REACHED', '已抵达')}</div></div>
                    </div>
                </div>}

            {}
            {gameState === 'VICTORY' && <div className="absolute inset-0 bg-black/90 z-50 flex flex-col items-center justify-center gap-6">
                    <div className="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-cyan-400 via-blue-500 to-purple-500 italic tracking-tight">{t('PROTOCOL COMPLETE', '协议完成')}</div>
                    <div className="text-slate-400 font-mono text-sm text-center max-w-md">{t('Network purged. System secured.', '网络已清除。系统已安全。')}<br />{t('ARIA Protocol — Mission Accomplished.', 'ARIA 协议 — 任务完成。')}</div>
                    <div className="flex gap-8 mt-4">
                        <div className="text-center"><div className="text-2xl font-bold text-white">{uiData.score.toLocaleString()}</div><div className="text-[10px] text-slate-500 font-mono">{t('FINAL SCORE', '最终分数')}</div></div>
                        <div className="text-center"><div className="text-2xl font-bold text-yellow-400">{uiData.level}</div><div className="text-[10px] text-slate-500 font-mono">{t('LEVEL', '等级')}</div></div>
                    </div>
                </div>}
        </div>;
};
window.GameConsole = ({
  onClose
}) => {
  const [activeApp, setActiveApp] = useState(null);
  const [lastScore, setLastScore] = useState(0);
  const [lang, setLang] = useState('cn');
  const [menuPos, setMenuPos] = useState({
    x: window.innerWidth / 2 - 200,
    y: window.innerHeight / 2 - 150
  });
  return <>
            <div className="fixed inset-0 pointer-events-none z-40 bg-transparent"></div>
            {activeApp === null && <DraggableWindow title="TDM LAUNCHER // SYSTEM ROOT" initialPos={menuPos} initialSize={{
      w: 400,
      h: 480
    }} onClose={onClose}>
                    <div className="h-full flex flex-col items-center justify-center space-y-6 bg-slate-950/50">
                        <div className="text-center">
                            <h1 className="text-5xl font-black italic tracking-tighter text-transparent bg-clip-text bg-gradient-to-br from-cyan-400 via-blue-600 to-purple-600 mb-2 drop-shadow-[0_0_20px_rgba(6,182,212,0.5)]">
                                PROTOCOL<br />OMEGA
                            </h1>
                            <p className="text-slate-500 font-mono tracking-[0.4em] text-[10px] uppercase">ARIA Defense Matrix V3.0</p>
                        </div>
                        {}
                        <div className="flex items-center gap-1 bg-slate-900/80 rounded-lg border border-slate-700/50 p-0.5">
                            <button onClick={() => setLang('en')} className={`px-3 py-1.5 rounded-md text-[10px] font-bold font-mono transition-all ${lang === 'en' ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/40' : 'text-slate-500 hover:text-slate-300 border border-transparent'}`}>ENGLISH</button>
                            <button onClick={() => setLang('cn')} className={`px-3 py-1.5 rounded-md text-[10px] font-bold font-mono transition-all ${lang === 'cn' ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/40' : 'text-slate-500 hover:text-slate-300 border border-transparent'}`}>中文</button>
                        </div>
                        <div className="flex flex-col gap-4 w-[280px]">
                            <button onClick={() => setActiveApp('void')} className="group h-24 bg-slate-900 border border-slate-800 hover:border-cyan-500 hover:bg-cyan-950/30 rounded-xl transition-all hover:-translate-y-1 hover:shadow-[0_0_20px_rgba(34,211,238,0.2)]">
                                <div className="flex flex-col items-center justify-center h-full">
                                    <div className="text-2xl font-bold text-cyan-400 italic mb-1 group-hover:scale-110 transition-transform">{lang === 'cn' ? '启动' : 'INITIATE'}</div>
                                    <span className="text-[10px] text-slate-500 font-mono group-hover:text-cyan-300/70">{lang === 'cn' ? '开始任务' : 'LAUNCH OPERATION'}</span>
                                </div>
                            </button>
                        </div>
                        {lastScore > 0 && <div className="text-center animate-in slide-in-from-bottom-5">
                                <div className="text-xs text-slate-500 font-mono mb-1">{lang === 'cn' ? '上次任务结果' : 'LAST OPERATION RESULT'}</div>
                                <div className="text-2xl font-bold text-white tracking-widest">{lastScore.toLocaleString()} PTS</div>
                            </div>}
                    </div>
                </DraggableWindow>}
            {activeApp === 'void' && <DraggableWindow title="VOID DEFENDER // PROTOCOL OMEGA" onClose={() => setActiveApp(null)} initialPos={{
      x: window.innerWidth / 2 - 450,
      y: window.innerHeight / 2 - 325
    }}>
                    <VoidDefender lang={lang} onGameOver={s => {
        setLastScore(s);
        setActiveApp(null);
      }} />
                </DraggableWindow>}
        </>;
};
