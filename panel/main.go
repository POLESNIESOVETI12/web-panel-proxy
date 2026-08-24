package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"html/template"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	panelUsers    = "/etc/tproxy-panel/users.json"
	panelProxies  = "/etc/tproxy-panel/profiles.json"
	tproxyProxies = "/etc/tproxy-server/profiles.json"
	configFile    = "/etc/tproxy-server/config.json"
	listen        = "127.0.0.1:8090"
)

type User struct {
	Username string `json:"username"`
	Hash     string `json:"password_hash"`
	Admin    bool   `json:"admin"`
}
type Profile struct {
	Name        string `json:"name"`
	Owner       string `json:"owner"`
	Secret      string `json:"secret"`
	Backend     string `json:"backend"`
	CarrierMode string `json:"carrier_mode,omitempty"`
}
type App struct {
	mu   sync.Mutex
	Host string
}

var nameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`)
var userRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$`)
var secretRe = regexp.MustCompile(`^(?:dd)?[0-9a-f]{32}$`)

const pageHTML = `<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Web Proxy Panel</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#0b0d12;color:#f6f7fb;font:15px system-ui,sans-serif}.wrap{max-width:1120px;margin:auto;padding:28px}
.top{display:flex;justify-content:space-between;gap:18px;align-items:center;margin-bottom:22px}h1{margin:0;font-size:25px}.muted,.small{color:#98a1b2}.small{font-size:12px}
.grid{display:grid;grid-template-columns:330px 1fr;gap:18px}.card{background:#121620;border:1px solid #232938;border-radius:16px;padding:18px}
label{display:block;color:#9aa3b4;font-size:13px;margin:14px 0 6px}input{width:100%;padding:11px;border-radius:10px;border:1px solid #30384a;background:#0c1017;color:#fff}
button{border:0;border-radius:10px;padding:10px 13px;cursor:pointer;background:#fff;color:#111;font-weight:600}.ghost{background:#1b2230;color:#dce3ef}.danger{background:#2b1114;color:#ff9ea8}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 9px;border-bottom:1px solid #252c3a;vertical-align:top}th{color:#9aa3b4;font-weight:500}
.mono{font:12px ui-monospace,monospace;word-break:break-all}.status{color:#65e6a4}.link{color:#b7cfff}.tools{display:flex;gap:7px;flex-wrap:wrap}.msg{min-height:20px;color:#ffd275;margin-top:10px}.hide{display:none}
@media(max-width:850px){.grid{grid-template-columns:1fr}.top{flex-direction:column;align-items:flex-start}}
</style></head><body><div class="wrap">
<div class="top"><div><h1>Telegram Web Proxy Panel</h1><div class="muted">Пользователи и Telegram Web Proxy профили</div></div><div class="small">{{.Host}}</div></div>
<div class="grid">
<div class="card">
<div style="display:flex;gap:8px;margin-bottom:14px"><button id="pb" onclick="switchTab('p')">Прокси</button>{{if .Admin}}<button id="ub" class="ghost" onclick="switchTab('u')">Пользователи</button>{{end}}</div>
<div id="pbox"><h3>Создать прокси</h3><label>Имя</label><input id="pn" placeholder="proxy-1">
<label>Secret</label><input id="ps" placeholder="автоматически">
<button style="width:100%;margin-top:12px" onclick="gen('ps',16)">Сгенерировать secret</button>
<button style="width:100%;margin-top:8px" onclick="createProxy()">Создать прокси</button><div id="pm" class="msg"></div>
</div>
{{if .Admin}}<div id="ubox" class="hide"><h3>Добавить пользователя</h3><label>Логин</label><input id="un" placeholder="alex">
<label>Пароль</label><input id="up" placeholder="автоматически">
<button style="width:100%;margin-top:12px" onclick="gen('up',18)">Сгенерировать пароль</button>
<button style="width:100%;margin-top:8px" onclick="createUser()">Создать пользователя</button><div id="um" class="msg"></div></div>{{end}}
</div>
<div class="card">
<h3 id="title">Мои Telegram Web Proxy</h3>
<table id="pt"><thead><tr><th>Имя</th><th>Secret</th><th>Telegram</th><th></th></tr></thead><tbody id="rows"></tbody></table>
{{if .Admin}}<table id="ut" class="hide"><thead><tr><th>Пользователь</th><th>Роль</th><th></th></tr></thead><tbody id="users"></tbody></table>{{end}}
</div></div></div>
<script>
const ADMIN={{.Admin}};
async function api(u,o){o=o||{};let r=await fetch(u,o);let t=await r.text();if(r.status===401){location.reload();return}if(!r.ok)throw Error(t||r.status);return JSON.parse(t)}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]) )}
function switchTab(x){pbox.classList.toggle('hide',x!='p');pt.classList.toggle('hide',x!='p');if(ADMIN){ubox.classList.toggle('hide',x!='u');ut.classList.toggle('hide',x!='u')}title.textContent=x=='p'?'Мои Telegram Web Proxy':'Пользователи';x=='p'?loadProxies():loadUsers()}
function gen(id,n){let a=new Uint8Array(n);crypto.getRandomValues(a);document.getElementById(id).value=[...a].map(x=>x.toString(16).padStart(2,'0')).join('')}
async function cp(v,b){await navigator.clipboard.writeText(v);let o=b.textContent;b.textContent='Скопировано ✓';setTimeout(()=>b.textContent=o,1200)}
async function createProxy(){try{let r=await api('./api/proxies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:pn.value.trim(),secret:ps.value.trim()})});pn.value='';ps.value='';pm.textContent='Прокси создан';loadProxies()}catch(e){pm.textContent=e.message}}
async function createUser(){try{let r=await api('./api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:un.value.trim(),password:up.value.trim()})});un.value='';up.value='';um.textContent='Создан. Пароль: '+r.password;loadUsers()}catch(e){um.textContent=e.message}}
async function delProxy(n){if(!confirm('Удалить '+n+'?'))return;await api('./api/proxies/'+encodeURIComponent(n),{method:'DELETE'});loadProxies()}
async function delUser(n){if(!confirm('Удалить '+n+'?'))return;await api('./api/users/'+encodeURIComponent(n),{method:'DELETE'});loadUsers()}
async function loadProxies(){let d=await api('./api/proxies');rows.innerHTML='';if(!d.profiles.length){rows.innerHTML='<tr><td colspan="4" class="muted">Прокси пока нет</td></tr>';return}d.profiles.forEach(p=>{let l='https://t.me/webproxy?server='+encodeURIComponent(d.host)+'&secret='+encodeURIComponent(p.secret.replace(/^dd/,''));let t=document.createElement('tr');t.innerHTML='<td><b>'+esc(p.name)+'</b><div class="small status">ACTIVE</div></td><td class="mono">'+esc(p.secret)+'<div class="tools"><button class="ghost">Copy</button></div></td><td><a class="link" target="_blank">Telegram</a><div class="tools"><button class="ghost">Copy URL</button></div></td><td><button class="danger">Удалить</button></td>';t.children[1].querySelector('button').onclick=e=>cp(p.secret,e.currentTarget);t.children[2].querySelector('a').href=l;t.children[2].querySelector('button').onclick=e=>cp(l,e.currentTarget);t.children[3].querySelector('button').onclick=()=>delProxy(p.name);rows.appendChild(t)})}
async function loadUsers(){if(!ADMIN)return;let d=await api('./api/users');users.innerHTML='';d.users.forEach(u=>{let t=document.createElement('tr');t.innerHTML='<td>'+esc(u.username)+'</td><td>'+(u.admin?'ADMIN':'USER')+'</td><td>'+(u.admin?'':'<button class="danger">Удалить</button>')+'</td>';if(!u.admin)t.children[2].querySelector('button').onclick=()=>delUser(u.username);users.appendChild(t)})}
loadProxies()
</script></body></html>`

func main() {
	a := &App{Host: readHost()}
	mux := http.NewServeMux()
	mux.HandleFunc("/", a.auth(a.page))
	mux.HandleFunc("/api/proxies", a.auth(a.proxies))
	mux.HandleFunc("/api/proxies/", a.auth(a.proxy))
	mux.HandleFunc("/api/users", a.admin(a.users))
	mux.HandleFunc("/api/users/", a.admin(a.user))
	s := &http.Server{Addr: listen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(s.ListenAndServe())
}

func (a *App) page(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	u, ok := authUser(r)
	if !ok {
		challenge(w)
		return
	}
	template.Must(template.New("p").Parse(pageHTML)).Execute(w, struct {
		Host  string
		Admin bool
	}{a.Host, u.Admin})
}
func challenge(w http.ResponseWriter) {
	w.Header().Set("WWW-Authenticate", `Basic realm="Telegram Web Proxy Panel"`)
	http.Error(w, "unauthorized", 401)
}
func authUser(r *http.Request) (User, bool) {
	un, p, ok := r.BasicAuth()
	if !ok {
		return User{}, false
	}
	return findUser(un, p)
}
func (a *App) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if _, ok := authUser(r); !ok {
			challenge(w)
			return
		}
		next(w, r)
	}
}
func (a *App) admin(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		u, ok := authUser(r)
		if !ok {
			challenge(w)
			return
		}
		if !u.Admin {
			http.Error(w, "forbidden", 403)
			return
		}
		next(w, r)
	}
}

func (a *App) proxies(w http.ResponseWriter, r *http.Request) {
	u, ok := authUser(r)
	if !ok {
		challenge(w)
		return
	}
	switch r.Method {
	case http.MethodGet:
		ps, _ := loadProfiles()
		out := make([]Profile, 0)
		for _, p := range ps {
			if u.Admin || p.Owner == u.Username {
				out = append(out, p)
			}
		}
		jsonOut(w, map[string]any{"host": a.Host, "profiles": out})
	case http.MethodPost:
		var in struct {
			Name, Secret string `json:"name"`
		}
		if json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&in) != nil {
			http.Error(w, "invalid json", 400)
			return
		}
		in.Name = strings.TrimSpace(in.Name)
		in.Secret = strings.ToLower(strings.TrimSpace(in.Secret))
		if !nameRe.MatchString(in.Name) {
			http.Error(w, "invalid name", 400)
			return
		}
		if in.Secret == "" {
			in.Secret = randomHex(16)
		}
		if !secretRe.MatchString(in.Secret) {
			http.Error(w, "invalid secret", 400)
			return
		}
		a.mu.Lock()
		defer a.mu.Unlock()
		ps, _ := loadProfiles()
		for _, p := range ps {
			if p.Owner == u.Username && p.Name == in.Name {
				http.Error(w, "profile already exists", 400)
				return
			}
		}
		ps = append(ps, Profile{Name: in.Name, Owner: u.Username, Secret: in.Secret, Backend: "127.0.0.1:2398", CarrierMode: "https"})
		if err := saveProfiles(ps); err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		jsonOut(w, map[string]any{"ok": true, "secret": in.Secret})
	default:
		http.Error(w, "method not allowed", 405)
	}
}
func (a *App) proxy(w http.ResponseWriter, r *http.Request) {
	u, ok := authUser(r)
	if !ok {
		challenge(w)
		return
	}
	if r.Method != http.MethodDelete {
		http.Error(w, "method not allowed", 405)
		return
	}
	name := strings.TrimPrefix(r.URL.Path, "/api/proxies/")
	if !nameRe.MatchString(name) {
		http.Error(w, "invalid name", 400)
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	ps, _ := loadProfiles()
	out := ps
	found := false
	for i := len(ps) - 1; i >= 0; i-- {
		if ps[i].Name == name && (u.Admin || ps[i].Owner == u.Username) {
			out = append(ps[:i], ps[i+1:]...)
			found = true
			break
		}
	}
	if !found {
		http.Error(w, "profile not found", 404)
		return
	}
	if err := saveProfiles(out); err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	jsonOut(w, map[string]any{"ok": true})
}
func (a *App) users(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		us, _ := loadUsers()
		out := make([]map[string]any, 0, len(us))
		for _, u := range us {
			out = append(out, map[string]any{"username": u.Username, "admin": u.Admin})
		}
		jsonOut(w, map[string]any{"users": out})
	case http.MethodPost:
		var in struct {
			Username, Password string `json:"username"`
		}
		if json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&in) != nil {
			http.Error(w, "invalid json", 400)
			return
		}
		in.Username = strings.TrimSpace(in.Username)
		in.Password = strings.TrimSpace(in.Password)
		if !nameRe.MatchString(in.Username) {
			http.Error(w, "invalid username", 400)
			return
		}
		if in.Password == "" {
			in.Password = randomHex(18)
		}
		if len(in.Password) < 12 {
			http.Error(w, "password must be at least 12 chars", 400)
			return
		}
		us, _ := loadUsers()
		for _, u := range us {
			if u.Username == in.Username {
				http.Error(w, "user exists", 400)
				return
			}
		}
		us = append(us, User{Username: in.Username, Hash: hash(in.Password), Admin: false})
		if err := saveUsers(us); err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		jsonOut(w, map[string]any{"ok": true, "username": in.Username, "password": in.Password})
	default:
		http.Error(w, "method not allowed", 405)
	}
}
func (a *App) user(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "method not allowed", 405)
		return
	}
	name := strings.TrimPrefix(r.URL.Path, "/api/users/")
	if !userRe.MatchString(name) || name == "admin" {
		http.Error(w, "invalid user", 400)
		return
	}
	us, _ := loadUsers()
	out := make([]User, 0, len(us))
	found := false
	for _, u := range us {
		if u.Username == name {
			found = true
			continue
		}
		out = append(out, u)
	}
	if !found {
		http.Error(w, "user not found", 404)
		return
	}
	if err := saveUsers(out); err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	jsonOut(w, map[string]any{"ok": true})
}

func findUser(name, password string) (User, bool) {
	us, err := loadUsers()
	if err != nil {
		return User{}, false
	}
	for _, u := range us {
		if u.Username == name && hash(password) == u.Hash {
			return u, true
		}
	}
	return User{}, false
}
func loadUsers() ([]User, error) {
	b, e := os.ReadFile(panelUsers)
	if e != nil {
		return nil, e
	}
	var u []User
	e = json.Unmarshal(b, &u)
	return u, e
}
func saveUsers(u []User) error {
	b, _ := json.MarshalIndent(u, "", "  ")
	return atomicWrite(panelUsers, b, 0600)
}
func loadProfiles() ([]Profile, error) {
	b, e := os.ReadFile(panelProxies)
	if e != nil {
		return nil, e
	}
	var p []Profile
	e = json.Unmarshal(b, &p)
	return p, e
}
func saveProfiles(p []Profile) error {
	b, _ := json.MarshalIndent(p, "", "  ")
	if e := atomicWrite(panelProxies, b, 0400); e != nil {
		return e
	}
	if e := atomicWrite(tproxyProxies, translateForTProxy(p), 0400); e != nil {
		return e
	}
	if e := os.Chown(panelProxies, 0, 0); e != nil {
		return e
	}
	if e := os.Chown(tproxyProxies, 0, groupID("tproxy")); e != nil {
		return e
	}
	if e := exec.Command("/usr/local/sbin/refresh-mtproxy-config").Run(); e != nil {
		return e
	}
	return exec.Command("systemctl", "restart", "tproxy-server.service").Run()
}
func translateForTProxy(p []Profile) []byte {
	type TP struct{ Name, Secret, Backend, CarrierMode string }
	x := make([]TP, 0, len(p))
	for _, v := range p {
		x = append(x, TP{v.Name, v.Secret, v.Backend, v.CarrierMode})
	}
	b, _ := json.MarshalIndent(map[string]any{"profiles": x}, "", "  ")
	return b
}
func atomicWrite(path string, b []byte, mode os.FileMode) error {
	if e := os.MkdirAll(filepath.Dir(path), 0750); e != nil {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if e != nil {
		return e
	}
	n := f.Name()
	defer os.Remove(n)
	if _, e = f.Write(b); e != nil {
		f.Close()
		return e
	}
	if e = f.Chmod(mode); e != nil {
		f.Close()
		return e
	}
	if e = f.Close(); e != nil {
		return e
	}
	return os.Rename(n, path)
}
func readHost() string {
	b, e := os.ReadFile(configFile)
	if e != nil {
		return "proxy.example.com"
	}
	var x struct {
		Host string `json:"public_hostname"`
	}
	if json.Unmarshal(b, &x) == nil && x.Host != "" {
		return x.Host
	}
	return "proxy.example.com"
}
func hash(s string) string { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:]) }
func randomHex(n int) string {
	b := make([]byte, n)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	return hex.EncodeToString(b)
}
func groupID(n string) int {
	b, e := os.ReadFile("/etc/group")
	if e != nil {
		return 0
	}
	for _, l := range strings.Split(string(b), "\n") {
		f := strings.Split(l, ":")
		if len(f) >= 3 && f[0] == n {
			var id int
			fmt.Sscanf(f[2], "%d", &id)
			return id
		}
	}
	return 0
}
func jsonOut(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}
