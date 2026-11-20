/* StreamClipper shared 3D motion background.
   Same engine as the login page, background-only (no card tilt):
   depth particle field + constellation links + three rotating wireframe
   polyhedra with glow + mouse parallax. Runs only when a #bg3d canvas
   exists on the page. Respects prefers-reduced-motion and pauses when
   the tab is hidden. */
(function () {
  var canvas = document.getElementById('bg3d');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var W = 0, H = 0, cx = 0, cy = 0, rafId = null;

  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = Math.max(1, W * dpr); canvas.height = Math.max(1, H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx = W / 2; cy = H / 2;
  }
  window.addEventListener('resize', resize);
  resize();

  // Mouse parallax (lerped)
  var mx = 0, my = 0, tmx = 0, tmy = 0;
  window.addEventListener('pointermove', function (e) {
    tmx = e.clientX / window.innerWidth - 0.5;
    tmy = e.clientY / window.innerHeight - 0.5;
  }, { passive: true });

  // --- 3D particle field ---
  var P = [];
  for (var i = 0; i < 130; i++) {
    P.push({ x: (Math.random() - 0.5) * 2, y: (Math.random() - 0.5) * 2, z: Math.random(), c: i % 3 });
  }
  var PCOLORS = ['rgba(226,232,240,', 'rgba(167,139,250,', 'rgba(251,113,133,'];

  // --- Wireframe polyhedra ---
  function icosahedron() {
    var t = (1 + Math.sqrt(5)) / 2;
    var v = [[-1,t,0],[1,t,0],[-1,-t,0],[1,-t,0],[0,-1,t],[0,1,t],[0,-1,-t],[0,1,-t],[t,0,-1],[t,0,1],[-t,0,-1],[-t,0,1]];
    var e = [[0,11],[0,5],[0,1],[0,10],[0,7],[1,5],[1,9],[1,8],[1,7],[2,11],[2,3],[2,4],[2,10],[2,6],[3,9],[3,8],[3,4],[3,6],[4,9],[4,5],[4,11],[5,9],[5,11],[6,10],[6,7],[6,8],[7,10],[7,8],[8,9],[10,11]];
    return { v: v, e: e };
  }
  function octahedron() {
    var v = [[1,0,0],[-1,0,0],[0,1,0],[0,-1,0],[0,0,1],[0,0,-1]];
    var e = [[0,2],[0,3],[0,4],[0,5],[1,2],[1,3],[1,4],[1,5],[2,4],[2,5],[3,4],[3,5]];
    return { v: v, e: e };
  }
  function cube() {
    var v = [];
    for (var x of [-1, 1]) for (var y of [-1, 1]) for (var z of [-1, 1]) v.push([x, y, z]);
    var e = [[0,1],[0,2],[0,4],[1,3],[1,5],[2,3],[2,6],[3,7],[4,5],[4,6],[5,7],[6,7]];
    return { v: v, e: e };
  }
  var shapes = [
    { geo: icosahedron(), fx: 0.70, fy: 0.30, r: 0.16, speed: 0.0042, color: '167,139,250', tilt: 0.42 },
    { geo: octahedron(),  fx: 0.15, fy: 0.74, r: 0.085, speed: -0.006, color: '251,113,133', tilt: 0.55 },
    { geo: cube(),        fx: 0.86, fy: 0.76, r: 0.055, speed: 0.008,  color: '34,211,238',  tilt: 0.35 },
  ];

  function project(x, y, z, ay, ax) {
    var cy1 = Math.cos(ay), sy1 = Math.sin(ay);
    var x1 = x * cy1 + z * sy1, z1 = -x * sy1 + z * cy1, y1 = y;
    var cx1 = Math.cos(ax), sx1 = Math.sin(ax);
    var y2 = y1 * cx1 - z1 * sx1, z2 = y1 * sx1 + z1 * cx1;
    var f = 3.4;
    var s = f / (f + z2);
    return { x: x1 * s, y: y2 * s, s: s };
  }

  function drawShape(sh, t) {
    var R = Math.min(W, H) * sh.r * (W < 640 ? 0.62 : 1);
    var px = W * sh.fx, py = H * sh.fy;
    var ay = t * sh.speed + 0.6, ax = sh.tilt;
    var pts = sh.geo.v.map(function (v) {
      var p = project(v[0], v[1], v[2], ay, ax);
      return { x: px + p.x * R + mx * 18, y: py + p.y * R + my * 14, s: p.s };
    });
    ctx.save();
    ctx.strokeStyle = 'rgba(' + sh.color + ',0.75)';
    ctx.fillStyle = 'rgba(' + sh.color + ',0.9)';
    ctx.lineWidth = 1.2;
    ctx.shadowColor = 'rgba(' + sh.color + ',0.9)';
    ctx.shadowBlur = 10;
    ctx.beginPath();
    sh.geo.e.forEach(function (ed) {
      var a = pts[ed[0]], b = pts[ed[1]];
      ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
    });
    ctx.stroke();
    pts.forEach(function (p) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, 1.8, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.restore();
  }

  var t = 0;
  function frame() {
    t += 1;
    mx += (tmx - mx) * 0.04; my += (tmy - my) * 0.04;
    ctx.clearRect(0, 0, W, H);

    // Particles (drift toward viewer, wrap)
    var proj = [];
    for (var i = 0; i < P.length; i++) {
      var p = P[i];
      p.z -= 0.0011; if (p.z <= 0.02) { p.z = 1; p.x = (Math.random() - 0.5) * 2; p.y = (Math.random() - 0.5) * 2; }
      var depth = 1 - p.z;
      var sx = cx + p.x * W * 0.5 * (1.05 - p.z * 0.55) + mx * 46 * depth;
      var sy = cy + p.y * H * 0.5 * (1.05 - p.z * 0.55) + my * 34 * depth;
      var r = depth * 2.1 + 0.4;
      ctx.fillStyle = PCOLORS[p.c] + (0.25 + depth * 0.65) + ')';
      ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2); ctx.fill();
      if (i < 50) proj.push({ x: sx, y: sy });
    }
    // Constellation links between near particles
    ctx.strokeStyle = 'rgba(167,139,250,0.10)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (var i = 0; i < proj.length; i++) {
      for (var j = i + 1; j < proj.length; j++) {
        var dx = proj[i].x - proj[j].x, dy = proj[i].y - proj[j].y;
        if (dx * dx + dy * dy < 10000) { ctx.moveTo(proj[i].x, proj[i].y); ctx.lineTo(proj[j].x, proj[j].y); }
      }
    }
    ctx.stroke();

    shapes.forEach(function (sh) { drawShape(sh, t); });

    if (!reduced) rafId = requestAnimationFrame(frame);
  }

  frame();
  if (!reduced) {
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { if (rafId) cancelAnimationFrame(rafId); rafId = null; }
      else if (!rafId) { rafId = requestAnimationFrame(frame); }
    });
  }
})();
