import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const COLORS = {
  scene: 0x0d1520,
  grid: 0x51657f,
  cell: 0x1a2737,
  obstacle: 0x465167,
  red: 0xec5b65,
  blue: 0x55a8eb,
  score: 0xf0ae55,
  visionRed: 0x48cd8e,
  visionBlue: 0x55a8eb,
  visionBoth: 0x4abeb0,
  fireStart: 0xffe28c,
  fireEnd: 0xf0ae55,
  scan: 0x5bc89a,
  hit: 0xec5b65,
};

const CELL_STEP = 1;
const TILE_SIZE = 0.965;
const ROBOT_Y = 0.004;
const ARENA_SCALE = 0.8;
const INITIAL_CAMERA_ZOOM = 1.5;
// The converted map's central platform is at y=0.  Keep the board a small
// distance above it so the backing and tile overlays do not z-fight with CAD
// surfaces.
const ARENA_SURFACE_Y = 0.025;
const POSITION_DURATION = 300;
const ROTATION_DURATION = 240;
const FACING_YAW = { N: Math.PI, E: Math.PI / 2, S: 0, W: -Math.PI / 2 };

function smoothstep(t) {
  return t * t * (3 - 2 * t);
}

function disposeObject(object) {
  object.traverse((child) => {
    child.geometry?.dispose();
    if (Array.isArray(child.material)) child.material.forEach(material => material.dispose());
    else child.material?.dispose();
  });
}

function cellKey(x, y) {
  return `${x},${y}`;
}

class RobotActor {
  constructor(scene, side, worldFromCell, labelTexture) {
    this.side = side;
    this.worldFromCell = worldFromCell;
    this.labelTexture = labelTexture;
    this.root = new THREE.Group();
    this.root.name = `${side === 'R' ? 'Red' : 'Blue'}SentryRoot`;
    this.root.visible = false;
    scene.add(this.root);

    const shadow = new THREE.Mesh(
      new THREE.CircleGeometry(0.38, 40),
      new THREE.MeshBasicMaterial({
        color: 0x05080c,
        transparent: true,
        opacity: 0.38,
        depthWrite: false,
      }),
    );
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = 0.008;
    shadow.renderOrder = 4;
    this.root.add(shadow);

    this.label = new THREE.Sprite(new THREE.SpriteMaterial({
      transparent: true,
      depthWrite: false,
      depthTest: true,
    }));
    this.label.position.y = 0.83;
    this.label.scale.set(0.68, 0.255, 1);
    this.label.renderOrder = 8;
    this.root.add(this.label);

    this.hasPose = false;
    this.wasVisible = false;
    this.modelReady = false;
    this.targetCell = null;
    this.targetFacing = null;
    this.positionTween = {
      active: false,
      from: new THREE.Vector3(),
      to: new THREE.Vector3(),
      startedAt: 0,
      duration: POSITION_DURATION,
    };
    this.yawTween = {
      active: false,
      from: 0,
      to: 0,
      startedAt: 0,
      duration: ROTATION_DURATION,
    };
  }

  attachModel(template) {
    const model = template.clone(true);
    model.name = `${this.side}SentryModel`;
    model.traverse((child) => {
      if (!child.isMesh) return;
      child.castShadow = false;
      child.receiveShadow = false;
    });
    this.root.add(model);
    this.modelReady = true;
    this.root.visible = this.wasVisible && this.hasPose;
  }

  sample(now) {
    if (this.positionTween.active) {
      const raw = (now - this.positionTween.startedAt) / this.positionTween.duration;
      const t = Math.min(1, Math.max(0, raw));
      this.root.position.lerpVectors(
        this.positionTween.from,
        this.positionTween.to,
        smoothstep(t),
      );
      if (t >= 1) this.positionTween.active = false;
    }
    if (this.yawTween.active) {
      const raw = (now - this.yawTween.startedAt) / this.yawTween.duration;
      const t = Math.min(1, Math.max(0, raw));
      this.root.rotation.y = THREE.MathUtils.lerp(
        this.yawTween.from,
        this.yawTween.to,
        smoothstep(t),
      );
      if (t >= 1) this.yawTween.active = false;
    }
  }

  snapPosition(target) {
    this.positionTween.active = false;
    this.root.position.copy(target);
  }

  snapYaw(target) {
    this.yawTween.active = false;
    this.root.rotation.y = target;
  }

  retargetPosition(target, now) {
    if (this.positionTween.to.distanceToSquared(target) < 1e-10 &&
        this.positionTween.active) return;
    this.positionTween.from.copy(this.root.position);
    this.positionTween.to.copy(target);
    this.positionTween.startedAt = now;
    this.positionTween.active = true;
  }

  retargetYaw(target, now) {
    if (this.targetFacing !== null && this.yawTween.active &&
        Math.abs(this.yawTween.to - target) < 1e-10) return;
    const current = this.root.rotation.y;
    const delta = Math.atan2(Math.sin(target - current), Math.cos(target - current));
    this.yawTween.from = current;
    this.yawTween.to = current + delta;
    this.yawTween.startedAt = now;
    this.yawTween.active = true;
  }

  sync(cell, facing, visible, { immediate = false, hard = false, animatePosition = false } = {}) {
    if (!cell || FACING_YAW[facing] === undefined) {
      this.root.visible = false;
      this.wasVisible = false;
      this.hasPose = false;
      return;
    }

    const now = performance.now();
    this.sample(now);
    const position = this.worldFromCell(cell[0], cell[1], ROBOT_Y);
    const yaw = FACING_YAW[facing];
    const becomingVisible = visible && !this.wasVisible;
    const mustSnap = immediate || hard || !this.hasPose || !visible || becomingVisible;
    const positionChanged = !this.targetCell ||
      this.targetCell[0] !== cell[0] || this.targetCell[1] !== cell[1];
    const facingChanged = this.targetFacing !== facing;

    if (mustSnap) {
      this.snapPosition(position);
      this.snapYaw(yaw);
    } else {
      if (positionChanged) {
        if (animatePosition) this.retargetPosition(position, now);
        else this.snapPosition(position);
      }
      if (facingChanged) this.retargetYaw(yaw, now);
    }

    this.targetCell = [cell[0], cell[1]];
    this.targetFacing = facing;
    this.label.material.map = this.labelTexture(this.side, facing);
    this.label.material.needsUpdate = true;
    this.hasPose = true;
    this.wasVisible = visible;
    this.root.visible = visible && this.modelReady;
  }

  reset() {
    this.positionTween.active = false;
    this.yawTween.active = false;
    this.root.visible = false;
    this.hasPose = false;
    this.wasVisible = false;
    this.targetCell = null;
    this.targetFacing = null;
  }

  debugState() {
    return {
      visible: this.root.visible,
      hasPose: this.hasPose,
      cell: this.targetCell,
      facing: this.targetFacing,
      position: this.root.position.toArray(),
      yaw: this.root.rotation.y,
      positionTweenActive: this.positionTween.active,
      yawTweenActive: this.yawTween.active,
      yawDelta: this.yawTween.to - this.yawTween.from,
    };
  }
}

export class Arena3D {
  constructor(container, { modelUrl, mapUrl, size = 7 } = {}) {
    this.container = container;
    this.modelUrl = modelUrl;
    this.mapUrl = mapUrl;
    this.size = size;
    this.cells = [];
    this.effects = [];
    this.fireCounts = [];
    this.labelTextures = new Map();
    this.mapReady = !mapUrl;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(COLORS.scene);

    this.camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
    this.defaultCameraPosition = new THREE.Vector3(9.2, 10.4, 12.2);
    this.humanCameraPosition = new THREE.Vector3(0, 13.6, 11.2);
    this.camera.position.copy(this.defaultCameraPosition);
    this.camera.zoom = INITIAL_CAMERA_ZOOM;
    this.camera.updateProjectionMatrix();

    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.08;
    this.renderer.domElement.setAttribute('aria-label', 'Three.js 3D match board');
    this.renderer.domElement.tabIndex = 0;
    container.prepend(this.renderer.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(0, 0.12, 0);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.enablePan = false;
    this.controls.minDistance = 6.5;
    this.controls.maxDistance = 32;
    this.controls.minPolarAngle = 0;
    this.controls.maxPolarAngle = Math.PI / 2;
    this.controls.update();

    const hemisphere = new THREE.HemisphereLight(0xd8e8f5, 0x26303b, 3.0);
    this.scene.add(hemisphere);
    const keyLight = new THREE.DirectionalLight(0xed9d92, 1.25);
    keyLight.position.set(-7, 4, 5);
    this.scene.add(keyLight);
    const fillLight = new THREE.DirectionalLight(0x8dbde8, 1.25);
    fillLight.position.set(7, 4, -5);
    this.scene.add(fillLight);

    this.mapGroup = new THREE.Group();
    this.mapGroup.name = 'RMUC2025MapBackground';
    this.mapGroup.position.y = 0;
    this.scene.add(this.mapGroup);

    this.arenaGroup = new THREE.Group();
    this.arenaGroup.name = 'ArenaOverlay';
    this.arenaGroup.position.y = ARENA_SURFACE_Y;
    this.arenaGroup.scale.setScalar(ARENA_SCALE);
    this.scene.add(this.arenaGroup);

    this.boardGroup = new THREE.Group();
    this.boardGroup.name = 'Board';
    this.arenaGroup.add(this.boardGroup);
    this.effectGroup = new THREE.Group();
    this.effectGroup.name = 'Effects';
    this.arenaGroup.add(this.effectGroup);
    this.buildBoard(size);

    const worldFromCell = (x, y, height = 0) => this.worldFromCell(x, y, height);
    const labelTexture = (side, facing) => this.getLabelTexture(side, facing);
    this.actors = {
      R: new RobotActor(this.arenaGroup, 'R', worldFromCell, labelTexture),
      B: new RobotActor(this.arenaGroup, 'B', worldFromCell, labelTexture),
    };

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();
    this.container.dataset.threeReady = 'true';
    this.container.__arena3d = this;

    this.renderer.setAnimationLoop((now) => this.animate(now));
    this.loadMap();
    this.loadModel();
  }

  buildBoard(size) {
    this.size = size;
    this.cells = [];
    this.fireCounts = new Array(size * size).fill(0);
    this.boardGroup.clear();

    const span = size * CELL_STEP;
    const backing = new THREE.Mesh(
      new THREE.BoxGeometry(span + 0.12, 0.035, span + 0.12),
      new THREE.MeshBasicMaterial({ color: COLORS.grid }),
    );
    backing.position.y = -0.018;
    backing.renderOrder = 0;
    this.boardGroup.add(backing);

    const tileGeometry = new THREE.PlaneGeometry(TILE_SIZE, TILE_SIZE);
    const overlayGeometry = new THREE.PlaneGeometry(TILE_SIZE, TILE_SIZE);
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const position = this.worldFromCell(x, y, 0);
        const tile = new THREE.Mesh(tileGeometry, new THREE.MeshBasicMaterial({
          color: COLORS.cell,
          transparent: true,
          opacity: 1,
          depthWrite: true,
        }));
        tile.rotation.x = -Math.PI / 2;
        tile.position.copy(position);
        tile.position.y = 0.004;
        tile.renderOrder = 1;
        this.boardGroup.add(tile);

        const vision = new THREE.Mesh(overlayGeometry, new THREE.MeshBasicMaterial({
          color: COLORS.visionRed,
          transparent: true,
          opacity: 0,
          depthWrite: false,
        }));
        vision.rotation.x = -Math.PI / 2;
        vision.position.copy(position);
        vision.position.y = 0.018;
        vision.visible = false;
        vision.renderOrder = 2;
        this.boardGroup.add(vision);

        const score = this.makeScoreOutline(position);
        score.visible = false;
        this.boardGroup.add(score);
        const obstacle = this.makeObstacleMark(position);
        obstacle.visible = false;
        this.boardGroup.add(obstacle);

        this.cells.push({ x, y, tile, vision, score, obstacle, visionWanted: false });
      }
    }
  }

  makeScoreOutline(position) {
    const inset = TILE_SIZE * 0.39;
    const points = [
      new THREE.Vector3(-inset, 0, -inset), new THREE.Vector3(inset, 0, -inset),
      new THREE.Vector3(inset, 0, -inset), new THREE.Vector3(inset, 0, inset),
      new THREE.Vector3(inset, 0, inset), new THREE.Vector3(-inset, 0, inset),
      new THREE.Vector3(-inset, 0, inset), new THREE.Vector3(-inset, 0, -inset),
    ];
    const geometry = new THREE.BufferGeometry().setFromPoints(points);
    const line = new THREE.LineSegments(geometry, new THREE.LineDashedMaterial({
      color: COLORS.score,
      transparent: true,
      opacity: 0.68,
      dashSize: 0.11,
      gapSize: 0.065,
      depthWrite: false,
    }));
    line.computeLineDistances();
    line.position.set(position.x, 0.03, position.z);
    line.renderOrder = 3;
    return line;
  }

  makeObstacleMark(position) {
    const points = [];
    for (const offset of [-0.13, 0.13]) {
      points.push(
        new THREE.Vector3(offset, 0, -0.28), new THREE.Vector3(offset, 0, 0.28),
        new THREE.Vector3(-0.28, 0, offset), new THREE.Vector3(0.28, 0, offset),
      );
    }
    const mark = new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(points),
      new THREE.LineBasicMaterial({ color: 0xf4f7fb, transparent: true, opacity: 0.9 }),
    );
    mark.position.set(position.x, 0.034, position.z);
    mark.renderOrder = 3;
    return mark;
  }

  getLabelTexture(side, facing) {
    const key = `${side}-${facing}`;
    if (this.labelTextures.has(key)) return this.labelTextures.get(key);
    const canvas = document.createElement('canvas');
    canvas.width = 256;
    canvas.height = 96;
    const context = canvas.getContext('2d');
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.font = '700 42px monospace';
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.lineWidth = 8;
    context.strokeStyle = 'rgba(13, 21, 32, 0.92)';
    context.strokeText(`${side} ${facing}`, 128, 48);
    context.fillStyle = '#ffffff';
    context.fillText(`${side} ${facing}`, 128, 48);
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    this.labelTextures.set(key, texture);
    return texture;
  }

  loadModel() {
    this.container.dataset.modelReady = 'false';
    new GLTFLoader().load(
      this.modelUrl,
      (gltf) => {
        const template = gltf.scene;
        template.updateMatrixWorld(true);
        this.actors.R.attachModel(template);
        this.actors.B.attachModel(template);
        this.container.dataset.modelReady = 'true';
        this.updateLoadingStatus();
        this.container.dispatchEvent(new CustomEvent('arena-model-ready'));
      },
      undefined,
      (error) => {
        this.container.dataset.modelError = 'true';
        this.updateLoadingStatus('车体模型加载失败');
        console.error('Failed to load sentry GLB', error);
      },
    );
  }

  loadMap() {
    this.container.dataset.mapReady = this.mapReady ? 'true' : 'false';
    if (!this.mapUrl) return;
    new GLTFLoader().load(
      this.mapUrl,
      (gltf) => {
        const map = gltf.scene;
        map.name = 'RMUC2025Map';
        map.traverse((child) => {
          if (!child.isMesh) return;
          child.castShadow = false;
          child.receiveShadow = true;
          child.renderOrder = -10;
          const materials = Array.isArray(child.material) ? child.material : [child.material];
          for (const material of materials) {
            if (!material) continue;
            material.depthWrite = true;
            material.needsUpdate = true;
          }
        });
        this.mapGroup.add(map);
        this.mapReady = true;
        this.container.dataset.mapReady = 'true';
        this.updateLoadingStatus();
        this.container.dispatchEvent(new CustomEvent('arena-map-ready'));
      },
      undefined,
      (error) => {
        this.container.dataset.mapError = 'true';
        this.updateLoadingStatus('地图背景加载失败');
        console.error('Failed to load RMUC map GLB', error);
      },
    );
  }

  updateLoadingStatus(errorText = '') {
    const status = this.container.querySelector('.arena-loading');
    if (!status) return;
    if (errorText) {
      status.textContent = errorText;
      return;
    }
    if (this.container.dataset.modelError === 'true') {
      status.textContent = '车体模型加载失败';
      return;
    }
    if (this.container.dataset.mapError === 'true') {
      status.textContent = '地图背景加载失败';
      return;
    }
    const modelReady = this.container.dataset.modelReady === 'true';
    const mapReady = this.container.dataset.mapReady === 'true';
    if (modelReady && mapReady) status.textContent = '';
    else if (!modelReady && !mapReady) status.textContent = '正在加载地图和车体...';
    else if (!mapReady) status.textContent = '正在加载地图背景...';
    else status.textContent = '正在加载车体模型...';
  }

  worldFromCell(x, y, height = 0) {
    const center = (this.size - 1) / 2;
    return new THREE.Vector3(
      (x - center) * CELL_STEP,
      height,
      (y - center) * CELL_STEP,
    );
  }

  resize() {
    const width = this.container.clientWidth;
    const height = this.container.clientHeight;
    if (!width || !height) return;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
  }

  setHumanView(enabled) {
    this.controls.enableRotate = !enabled;
    this.camera.position.copy(enabled ? this.humanCameraPosition : this.defaultCameraPosition);
    this.controls.target.set(0, 0.12, 0);
    this.controls.update();
  }

  updateBoard(board, options = {}) {
    if (!board) {
      this.clearState();
      return;
    }
    if (board.size && board.size !== this.size) this.buildBoard(board.size);

    const obstacleKeys = new Set((board.obstacles || []).map(([x, y]) => cellKey(x, y)));
    const scoreKeys = new Set((board.score_zones || []).map(([x, y]) => cellKey(x, y)));
    const redVision = new Set((options.redVisionCells || []).map(([x, y]) => cellKey(x, y)));
    const blueVision = new Set((options.blueVisionCells || []).map(([x, y]) => cellKey(x, y)));
    const redCell = options.showRed && board.red_pos ? cellKey(...board.red_pos) : null;
    const blueCell = options.showBlue && board.blue_pos ? cellKey(...board.blue_pos) : null;

    for (let index = 0; index < this.cells.length; index++) {
      const cell = this.cells[index];
      const key = cellKey(cell.x, cell.y);
      let color = COLORS.cell;
      let opacity = 1;
      if (key === redCell) { color = COLORS.red; opacity = 0.68; }
      if (key === blueCell) { color = COLORS.blue; opacity = 0.68; }
      if (obstacleKeys.has(key)) { color = COLORS.obstacle; opacity = 1; }
      if (scoreKeys.has(key)) { color = COLORS.score; opacity = 0.18; }
      cell.tile.material.color.setHex(color);
      cell.tile.material.opacity = opacity;
      cell.obstacle.visible = obstacleKeys.has(key);
      cell.score.visible = scoreKeys.has(key);

      const seesRed = redVision.has(key);
      const seesBlue = blueVision.has(key);
      cell.visionWanted = seesRed || seesBlue;
      cell.vision.visible = cell.visionWanted && this.fireCounts[index] === 0;
      if (seesRed && seesBlue) {
        cell.vision.material.color.setHex(COLORS.visionBoth);
        cell.vision.material.opacity = 0.36;
      } else if (seesRed) {
        cell.vision.material.color.setHex(COLORS.visionRed);
        cell.vision.material.opacity = 0.26;
      } else if (seesBlue) {
        cell.vision.material.color.setHex(COLORS.visionBlue);
        cell.vision.material.opacity = 0.26;
      }
    }

    const hardSides = new Set(options.hardSides || []);
    const movingSide = options.event?.type === 'action' && options.event.success &&
      ['move', 'back'].includes(options.event.action) ? options.event.side : null;
    this.actors.R.sync(board.red_pos, board.red_facing, Boolean(options.showRed), {
      immediate: Boolean(options.immediate),
      hard: hardSides.has('R'),
      animatePosition: movingSide === 'R',
    });
    this.actors.B.sync(board.blue_pos, board.blue_facing, Boolean(options.showBlue), {
      immediate: Boolean(options.immediate),
      hard: hardSides.has('B'),
      animatePosition: movingSide === 'B',
    });
  }

  clearState() {
    for (const cell of this.cells) {
      cell.tile.material.color.setHex(COLORS.cell);
      cell.tile.material.opacity = 1;
      cell.visionWanted = false;
      cell.vision.visible = false;
      cell.score.visible = false;
      cell.obstacle.visible = false;
    }
    this.actors.R.reset();
    this.actors.B.reset();
    this.clearEffects();
  }

  reset() {
    this.clearState();
  }

  addEffect(object, duration, update, delay = 0, onDone = null) {
    object.visible = delay === 0;
    this.effectGroup.add(object);
    this.effects.push({
      object,
      duration,
      update,
      delay,
      startedAt: performance.now(),
      onDone,
    });
  }

  clearEffects() {
    for (const effect of this.effects) {
      this.effectGroup.remove(effect.object);
      disposeObject(effect.object);
    }
    this.effects = [];
    this.fireCounts.fill(0);
  }

  flashFire(origin, target, cells) {
    for (const [x, y] of cells) {
      const index = y * this.size + x;
      if (!this.cells[index]) continue;
      this.fireCounts[index] += 1;
      this.cells[index].vision.visible = false;
      const material = new THREE.MeshBasicMaterial({
        color: COLORS.fireStart,
        transparent: true,
        opacity: 0.8,
        depthWrite: false,
      });
      const plane = new THREE.Mesh(new THREE.PlaneGeometry(TILE_SIZE, TILE_SIZE), material);
      plane.rotation.x = -Math.PI / 2;
      plane.position.copy(this.worldFromCell(x, y, 0.052));
      plane.renderOrder = 6;
      this.addEffect(plane, 380, (t) => {
        material.color.lerpColors(
          new THREE.Color(COLORS.fireStart),
          new THREE.Color(COLORS.fireEnd),
          t,
        );
        material.opacity = THREE.MathUtils.lerp(0.8, 0.2, t);
      }, 0, () => {
        this.fireCounts[index] = Math.max(0, this.fireCounts[index] - 1);
        if (this.fireCounts[index] === 0) this.cells[index].vision.visible = this.cells[index].visionWanted;
      });
    }

    if (!origin || !target) return;
    const start = this.worldFromCell(origin[0], origin[1], 0.64);
    const end = this.worldFromCell(target[0], target[1], 0.64);
    const direction = new THREE.Vector3().subVectors(end, start);
    const length = direction.length();
    if (!length) return;
    const beam = new THREE.Group();
    const glowMaterial = new THREE.MeshBasicMaterial({
      color: COLORS.fireEnd,
      transparent: true,
      opacity: 0.76,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const coreMaterial = new THREE.MeshBasicMaterial({
      color: 0xfff3c6,
      transparent: true,
      opacity: 1,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    beam.add(new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, length, 12), glowMaterial));
    beam.add(new THREE.Mesh(new THREE.CylinderGeometry(0.011, 0.011, length, 8), coreMaterial));
    beam.position.copy(start).add(end).multiplyScalar(0.5);
    beam.quaternion.setFromUnitVectors(
      new THREE.Vector3(0, 1, 0),
      direction.normalize(),
    );
    beam.renderOrder = 9;
    this.addEffect(beam, 380, (t) => {
      glowMaterial.opacity = 0.76 * (1 - t);
      coreMaterial.opacity = 1 - t;
    });
  }

  flashScan(position) {
    if (!position) return;
    for (let radius = 1; radius <= 5; radius++) {
      const material = new THREE.MeshBasicMaterial({
        color: COLORS.scan,
        transparent: true,
        opacity: 0.8,
        side: THREE.DoubleSide,
        depthWrite: false,
      });
      const ring = new THREE.Mesh(
        new THREE.RingGeometry(radius - 0.025, radius + 0.025, 96),
        material,
      );
      ring.rotation.x = -Math.PI / 2;
      ring.position.copy(this.worldFromCell(position[0], position[1], 0.09));
      ring.scale.setScalar(0.1);
      ring.renderOrder = 7;
      this.addEffect(ring, 1200, (t) => {
        const scale = THREE.MathUtils.lerp(0.1, 1, smoothstep(t));
        ring.scale.setScalar(scale);
        material.opacity = 0.8 * (1 - t);
      }, radius * 80);
    }
  }

  flashMove(position) {
    if (!position) return;
    const material = new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: 0.38,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(TILE_SIZE, TILE_SIZE), material);
    plane.rotation.x = -Math.PI / 2;
    plane.position.copy(this.worldFromCell(position[0], position[1], 0.058));
    plane.renderOrder = 7;
    this.addEffect(plane, 200, t => { material.opacity = 0.38 * (1 - t); });
  }

  flashKill(position) {
    if (!position) return;
    const hitMaterial = new THREE.MeshBasicMaterial({
      color: COLORS.hit,
      transparent: true,
      opacity: 0,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const hit = new THREE.Mesh(new THREE.PlaneGeometry(TILE_SIZE, TILE_SIZE), hitMaterial);
    hit.rotation.x = -Math.PI / 2;
    hit.position.copy(this.worldFromCell(position[0], position[1], 0.066));
    hit.renderOrder = 8;
    this.addEffect(hit, 650, (t) => {
      const pulse = Math.sin(Math.PI * t);
      hitMaterial.opacity = 0.86 * pulse;
      hit.scale.setScalar(1 + 0.06 * pulse);
    });

    const points = [];
    for (let index = 0; index < 12; index++) {
      const angle = index * Math.PI / 6;
      points.push(
        new THREE.Vector3(Math.cos(angle) * 0.08, 0, Math.sin(angle) * 0.08),
        new THREE.Vector3(Math.cos(angle) * 0.42, 0, Math.sin(angle) * 0.42),
      );
    }
    const burstMaterial = new THREE.LineBasicMaterial({
      color: 0xffe2ad,
      transparent: true,
      opacity: 1,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const burst = new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(points),
      burstMaterial,
    );
    burst.position.copy(this.worldFromCell(position[0], position[1], 0.52));
    burst.scale.setScalar(0.25);
    burst.renderOrder = 10;
    this.addEffect(burst, 750, (t) => {
      burst.scale.setScalar(THREE.MathUtils.lerp(0.25, 1.5, smoothstep(t)));
      burstMaterial.opacity = 1 - t;
    });
  }

  animate(now) {
    this.actors.R.sample(now);
    this.actors.B.sample(now);
    for (let index = this.effects.length - 1; index >= 0; index--) {
      const effect = this.effects[index];
      const elapsed = now - effect.startedAt - effect.delay;
      if (elapsed < 0) continue;
      effect.object.visible = true;
      const t = Math.min(1, elapsed / effect.duration);
      effect.update(t);
      if (t < 1) continue;
      effect.onDone?.();
      this.effectGroup.remove(effect.object);
      disposeObject(effect.object);
      this.effects.splice(index, 1);
    }
    this.controls.update();
    if (this.container.offsetParent !== null) this.renderer.render(this.scene, this.camera);
  }

  getDebugState() {
    return {
      modelReady: this.container.dataset.modelReady === 'true',
      mapReady: this.container.dataset.mapReady === 'true',
      camera: this.camera.position.toArray(),
      cameraZoom: this.camera.zoom,
      target: this.controls.target.toArray(),
      arenaScale: this.arenaGroup.scale.x,
      red: this.actors.R.debugState(),
      blue: this.actors.B.debugState(),
      effects: this.effects.length,
      drawCalls: this.renderer.info.render.calls,
      triangles: this.renderer.info.render.triangles,
    };
  }
}
