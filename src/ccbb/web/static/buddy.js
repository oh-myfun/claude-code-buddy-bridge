// Buddy Pet Renderer — ASCII art animation engine for CCBB approval page
// Species data is loaded from buddies/*.js files (each adds to SPECIES object)

const BC = {
  DIM:'#A87060', WHITE:'#FFFFFF', YEL:'#FFE060', HEART:'#F81F50',
  CYAN:'#07FFFF', GREEN:'#07E000', PURPLE:'#A01FFF', RED:'#F80000', BLUE:'#041FFF'
};

const SPECIES_LIST = Object.keys(SPECIES);

function randomSpecies(exclude) {
  const pool = SPECIES_LIST.filter(s => s !== exclude);
  return pool[Math.floor(Math.random() * pool.length)];
}

class BuddyRenderer {
  constructor() {
    this.species = SPECIES_LIST[Math.floor(Math.random() * SPECIES_LIST.length)];
    this.state = 'sleep';
    this.tick = 0;
    this.baseState = 'sleep';
    this.timer = null;
    this.idleTimer = null;
    this.stateTimeout = null;
    this._pm = {};
  }

  start() {
    $('buddy-species').textContent = this.species;
    this.timer = setInterval(() => this._tick(), 200);
  }

  setState(state, opts) {
    this.state = state;
    clearTimeout(this.stateTimeout);
    if (opts && opts.duration) {
      this.stateTimeout = setTimeout(() => {
        this.state = (opts.revertTo) || this.baseState;
        this.stateTimeout = null;
      }, opts.duration);
    }
    this._resetIdle();
  }

  setBaseState(state) {
    this.baseState = state;
    if (!this.stateTimeout) this.state = state;
  }

  _resetIdle() {
    clearTimeout(this.idleTimer);
    if (this.baseState === 'idle' && this.state === 'idle') {
      this.idleTimer = setTimeout(() => {
        this.setBaseState('sleep');
        this.state = 'sleep';
      }, 30000);
    }
  }

  _tick() {
    this.tick++;
    this._pm = {};
    const sp = SPECIES[this.species];
    if (!sp) return;
    const st = sp.states[this.state];
    if (!st) return;
    const beat = Math.floor(this.tick / st.tickDiv) % st.seq.length;
    const poseIdx = st.seq[beat];
    if (poseIdx >= st.poses.length) return;
    const pose = st.poses[poseIdx];
    const grid = pose.map(l => [...l]);
    this._overlayParticles(grid);
    this._render(grid, sp.color);
    this._applyTransform(st, beat);
  }

  _mk(r,c,color) { this._pm[r+','+c] = color; }

  _overlayParticles(grid) {
    const s = this.state;
    if (s === 'sleep') this._sleepZ(grid);
    else if (s === 'busy') this._busyTicker(grid);
    else if (s === 'attention') this._attentionBang(grid);
    else if (s === 'celebrate') this._celebrateConfetti(grid);
    else if (s === 'dizzy') this._dizzyStars(grid);
    else if (s === 'heart') this._heartStream(grid);
  }

  _sleepZ(grid) {
    const t = this.tick, sp = this.species;
    const mod = (sp==='capybara'||sp==='duck'||sp==='blob'||sp==='dragon') ? 10 : 12;
    const dir = (sp==='snail') ? -1 : 1;
    const p1 = t%mod, p2 = (t+4)%mod, p3 = (t+7)%mod;
    const c1 = (sp==='robot'||sp==='penguin') ? BC.CYAN : BC.DIM;
    const c2 = (sp==='robot') ? BC.CYAN : BC.WHITE;
    const r1=Math.max(0,2-Math.floor(p1/4)), co1=Math.max(0,Math.min(11,10+dir*Math.floor(p1/2)));
    if(r1<5&&co1<12){grid[r1][co1]='z';this._mk(r1,co1,c1);}
    const r2=Math.max(0,1-Math.floor(p2/5)), co2=Math.max(0,Math.min(11,11+dir*Math.floor(p2/3)));
    if(r2<5&&co2<12){grid[r2][co2]='Z';this._mk(r2,co2,c2);}
    const r3=Math.max(0,1-Math.floor(p3/6)), co3=Math.max(0,Math.min(11,8+dir*Math.floor(p3/4)));
    if(r3<5&&co3<12){grid[r3][co3]='z';this._mk(r3,co3,BC.DIM);}
  }

  _busyTicker(grid) {
    const t = this.tick, sp = this.species;
    let chars, color;
    if(sp==='duck'){chars=['o  ','oO ','oOo',' Oo','  o','   '];color=BC.CYAN;}
    else if(sp==='goose'){chars=['h  ','ho ','hon','onk','nk!','k! ','!  ','   '];color=BC.YEL;}
    else if(sp==='dragon'){chars=['$  ','$$ ','$$$',' $$','  $','   '];color=BC.YEL;}
    else if(sp==='robot'){chars=['1  ','10 ','101','010','10 ','1  '];color=BC.GREEN;}
    else if(sp==='ghost'){chars=['o  ','oO ','oOo',' Oo','  o','   '];color=BC.CYAN;}
    else if(sp==='chonk'){chars=['+  ','x  ','*  ','x  '];color=BC.CYAN;}
    else if(sp==='octopus'||sp==='snail'){chars=['.  ','.. ','...',' ..','  .','   '];color=BC.CYAN;}
    else if(sp==='cactus'){chars=['.  ','.. ','...',' ..','  .','   '];color=BC.GREEN;}
    else{chars=['.  ','.. ','...',' ..','  .','   '];color=BC.WHITE;}
    const ticker = chars[t % chars.length];
    for(let i=0;i<3;i++){
      if(ticker[i]!==' '&&9+i<12){grid[2][9+i]=ticker[i];this._mk(2,9+i,color);}
    }
  }

  _attentionBang(grid) {
    const t = this.tick, sp = this.species;
    const c1=(sp==='goose'||sp==='chonk')?BC.RED:BC.YEL;
    const c2=(sp==='dragon'||sp==='robot'||sp==='chonk')?BC.RED:BC.YEL;
    if((t>>1)&1){grid[0][4]='!';this._mk(0,4,c1);}
    if(Math.floor(t/3)&1){grid[0][7]='!';this._mk(0,7,c2);}
    if(sp==='goose'&&Math.floor(t/4)&1){grid[1][11]='!';this._mk(1,11,BC.RED);}
    if((sp==='blob'||sp==='chonk')&&Math.floor(t/4)&1){grid[0][1]='!';this._mk(0,1,BC.YEL);}
  }

  _celebrateConfetti(grid) {
    const t=this.tick, sp=this.species;
    const cols=(sp==='chonk')?[BC.YEL,BC.HEART,BC.CYAN,BC.WHITE,BC.GREEN,BC.PURPLE]:[BC.YEL,BC.HEART,BC.CYAN,BC.WHITE,BC.GREEN];
    const cnt=(sp==='goose'||sp==='blob'||sp==='chonk')?7:6;
    const g1=(sp==='dragon')?'$':(sp==='robot')?'+':(sp==='duck')?'~':'*';
    const g2=(sp==='ghost'||sp==='octopus'||sp==='chonk')?'o':'.';
    for(let i=0;i<cnt;i++){
      const phase=(t*2+i*11)%22;
      if(phase<5){const col=Math.min(11,i*2);const ch=(i+Math.floor(t/2))&1?g1:g2;grid[0][col]=ch;this._mk(0,col,cols[i%cols.length]);}
    }
  }

  _dizzyStars(grid) {
    const t=this.tick, sp=this.species;
    const OX=(sp==='chonk')?[0,6,9,6,0,-6,-9,-6]:[0,5,7,5,0,-5,-7,-5];
    const OY=(sp==='chonk')?[-6,-4,0,4,6,4,0,-4]:[-5,-3,0,3,5,3,0,-3];
    const p1=t%8, p2=(t+4)%8;
    const ch1=(sp==='robot')?'?':'*', co1=(sp==='octopus')?BC.CYAN:BC.CYAN;
    const r1=Math.max(0,Math.min(4,2+Math.round(OY[p1]/3))), c1=Math.max(0,Math.min(11,6+Math.round(OX[p1]/2)));
    grid[r1][c1]=ch1; this._mk(r1,c1,co1);
    const ch2=(sp==='robot')?'x':'*', co2=(sp==='octopus')?BC.PURPLE:BC.YEL;
    const r2=Math.max(0,Math.min(4,2+Math.round(OY[p2]/3))), c2=Math.max(0,Math.min(11,6+Math.round(OX[p2]/2)));
    grid[r2][c2]=ch2; this._mk(r2,c2,co2);
    if(sp==='goose'||sp==='blob'||sp==='dragon'||sp==='chonk'||sp==='axolotl'){
      const p3=(t+2)%8;
      let ch3,co3;
      if(sp==='goose'){ch3='+';co3=BC.PURPLE;}else if(sp==='blob'){ch3='o';co3=BC.WHITE;}
      else if(sp==='dragon'){ch3='$';co3=BC.WHITE;}else if(sp==='chonk'){ch3='+';co3=BC.WHITE;}
      else{ch3='o';co3=BC.HEART;}
      const r3=Math.max(0,Math.min(4,2+Math.round(OY[p3]/3))), c3=Math.max(0,Math.min(11,6+Math.round(OX[p3]/2)));
      grid[r3][c3]=ch3; this._mk(r3,c3,co3);
    }
  }

  _heartStream(grid) {
    const t=this.tick, sp=this.species;
    const cnt=(sp==='chonk')?6:5;
    for(let i=0;i<cnt;i++){
      const mod=(sp==='chonk')?18:16;
      const phase=(t+i*((sp==='chonk')?3:4))%mod;
      const row=4-Math.floor(phase/4);
      const col=Math.max(0,Math.min(11,2+i*2+(((Math.floor(phase/3)&1)*2)-1)));
      if(row>=0&&row<5){grid[row][col]='v';this._mk(row,col,BC.HEART);}
    }
  }

  _render(grid, bodyColor) {
    const parts = [];
    for (let r = 0; r < grid.length; r++) {
      for (let c = 0; c < grid[r].length; c++) {
        const ch = grid[r][c];
        if (ch === ' ') { parts.push(' '); }
        else {
          const color = this._pm[r+','+c] || bodyColor;
          const esc = ch==='< '?'&lt;':ch==='>'?'&gt;':ch==='&'?'&amp;':ch;
          parts.push('<span style="color:'+color+'">'+esc+'</span>');
        }
      }
      if (r < grid.length - 1) parts.push('\n');
    }
    $('buddy-sprite').innerHTML = parts.join('');
  }

  _applyTransform(st, beat) {
    const stage = $('buddy-stage');
    if (!stage) return;
    let y = 0, x = 0;
    if (st.yShift) y = st.yShift[beat] || 0;
    if (st.xShift) x = st.xShift[beat] || 0;
    if (st.yBob) y = st.yBob[beat] || 0;
    if (this.state === 'attention') {
      const poseIdx = st.seq[beat];
      if (poseIdx === 4) {
        const mag = (this.species==='goose'||this.species==='chonk') ? 2 : 1;
        x = (this.tick & 1) ? mag : -mag;
      } else if (this.species === 'goose' && poseIdx === 3) {
        x = (this.tick & 1) ? 1 : -1;
      }
    }
    stage.style.transform = (x || y) ? 'translate('+x+'px,'+y+'px)' : '';
  }
}

const buddy = new BuddyRenderer();
