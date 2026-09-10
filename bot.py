"""Business booking assistant 1.2. Python stdlib; all times Europe/Moscow."""
import os, re, json, time, sqlite3, threading, hashlib, hmac, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.2'
STATUS = {'free':'свободно','blocked':'занято вручную','held':'бронь','booked':'оплачено','review':'проверка поста','payment':'ожидаем оплату','checking':'проверка оплаты','expired':'срок истёк','confirmed':'оплачено','cancelled':'отменено'}
TZ = ZoneInfo('Europe/Moscow')
MENU = {'keyboard':[['Расписание','Добавить слоты'],['Заявки','Шаблоны'],['Цены и дополнения'],['Автоответы','Помощь']], 'resize_keyboard':True}
DEFAULTS = {
 'welcome':'Здравствуйте! Пришлите, пожалуйста, объявление, которое хотите разместить 🤍 Если пост ещё не готов, можно сначала посмотреть стоимость и свободное время.',
 'slots':'Свободное время для публикации (Москва). Выберите подходящий слот:',
 'empty':'Сейчас свободное время не указано. Уточню расписание и отвечу вам лично.',
 'held':'Вы выбрали {slot}. Время закрепляется после подтверждения оплаты.',
 'post':'Объявление получила, спасибо! Сейчас проверю. Если всё в порядке, пришлю реквизиты для оплаты 🤍',
 'payment':'', 'pricing':'{catalog}', 'conditions':'Пришлите объявление одним сообщением: текст с фото или альбом до 10 фотографий с текстом в подписи. Укажите услугу, стоимость, дату, адрес, контакт для записи и требования к модели, если они есть. Пост остаётся в канале; удаление — по вашему запросу. Перенос или отмену согласуйте заранее.',
 'approved':'Размещение {slot}. Стоимость: {price} ₽. Слот закрепляется после подтверждения оплаты администратором. После оплаты пришлите чек.',
 'paid_claim':'Спасибо! Проверю поступление оплаты и подтвержу размещение.',
 'confirmed':'Оплата подтверждена. Ваше размещение: {slot}.',
 'cancelled':'Заявка на {slot} отменена. Для выбора другого времени напишите «свободные слоты».',
}
LABELS={'welcome':'Приветствие','slots':'Свободное время','empty':'Нет слотов','held':'Выбор времени','post':'Пост получен','payment':'Реквизиты','pricing':'Прайс','conditions':'Условия','approved':'Счёт','paid_claim':'Проверка оплаты','confirmed':'Оплата подтверждена','cancelled':'Отмена заявки'}
DEFAULT_TRIGGERS={
 'slots':['ближайший слот','свободные слоты','свободное время','ближайшее время','какое время','когда можно','позже','другой день','другое время','завтра'],
 'pricing':['сколько стоит','стоимость размещения','цена размещения','прайс','расценки'],
 'conditions':['как разместить','как разместиться','условия размещения','хочу разместить','хочу разместиться'],
 'paid_claim':['оплатила','оплатил','оплата прошла','перевела','перевел','перевёл'],
}

def stamp(ts): return datetime.fromtimestamp(ts,TZ).strftime('%d.%m.%Y в %H:%M') if ts else 'время ещё не выбрано'
def buttons(rows): return {'inline_keyboard':[[{'text':label,'callback_data':value} for label,value in row] for row in rows]}
def norm(t): return ' '.join(re.findall(r'[\w]+',t.lower().replace('ё','е')))

def api(token,method,payload):
    req=urllib.request.Request('https://api.telegram.org/bot'+token+'/'+method,json.dumps(payload).encode(),{'Content-Type':'application/json','User-Agent':'BusinessBookingBot/1.2'})
    try:
        with urllib.request.urlopen(req,timeout=30) as r: result=json.load(r)
    except urllib.error.HTTPError as e:
        if e.code==429:
            try: delay=json.load(e).get('parameters',{}).get('retry_after',30)
            except Exception: delay=30
            raise RateLimit(int(delay)) from None
        raise RuntimeError('Telegram HTTP '+str(e.code)) from None
    except Exception:
        raise RuntimeError('Telegram: delivery outcome unknown') from None
    if not result.get('ok'): raise RuntimeError('Telegram rejected request')
    return result['result']
class RateLimit(Exception):
    def __init__(self,delay): self.delay=delay

class App:
    def __init__(self,path,owner,token='',sender=None,clock=time.time):
        self.owner=int(owner or 0);self.token=token;self.clock=clock;self.automatic=False
        self.sender=sender or (lambda m,p:api(token,m,p))
        self.lock=threading.RLock();self.event=threading.Event();self.stopping=threading.Event()
        self.db=sqlite3.connect(path,check_same_thread=False);self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT);
        CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY,active INTEGER);
        CREATE TABLE IF NOT EXISTS slots(id INTEGER PRIMARY KEY,at REAL UNIQUE,status TEXT DEFAULT 'free');
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY,slot INTEGER,conn TEXT,chat INTEGER,name TEXT,status TEXT,deadline REAL,price TEXT DEFAULT '',notified INTEGER DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS single_hold ON bookings(conn,chat) WHERE status IN ('review','payment','checking','expired');
        CREATE TABLE IF NOT EXISTS pauses(conn TEXT,chat INTEGER,until REAL,PRIMARY KEY(conn,chat));
        CREATE TABLE IF NOT EXISTS sessions(chat INTEGER PRIMARY KEY,mode TEXT);
        CREATE TABLE IF NOT EXISTS templates(k TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY,payload TEXT,status TEXT DEFAULT 'pending',at REAL);
        CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,method TEXT,payload TEXT,status TEXT DEFAULT 'pending',after REAL DEFAULT 0,scope TEXT DEFAULT '',automatic INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS cooldown(conn TEXT,chat INTEGER,kind TEXT,at REAL,PRIMARY KEY(conn,chat,kind));''')
        # Versioned migration: keep existing payments/settings, remove temporary holds.
        migrated=self.db.execute("SELECT v FROM settings WHERE k='schema_version'").fetchone()
        legacy=not migrated and bool(self.db.execute('SELECT 1 FROM templates LIMIT 1').fetchone())
        if legacy:
            backup=sqlite3.connect(str(path)+'.before-1.1.sqlite3')
            try:self.db.backup(backup)
            finally:backup.close()
        with self.db:
            if not migrated:
                self.db.execute('DROP INDEX IF EXISTS occupied')
                self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS confirmed_slot ON bookings(slot) WHERE status='confirmed'")
                self.db.execute("UPDATE slots SET status='free' WHERE status='held' AND NOT EXISTS (SELECT 1 FROM bookings WHERE slot=slots.id AND status='confirmed')")
                self.db.execute("UPDATE slots SET status='booked' WHERE EXISTS (SELECT 1 FROM bookings WHERE slot=slots.id AND status='confirmed')")
                self.db.execute("UPDATE bookings SET status=CASE WHEN price='' THEN 'review' ELSE 'payment' END WHERE status='expired'")
                self.db.execute('UPDATE bookings SET deadline=0,notified=0')
                if legacy:
                    # Old queued invoices may promise a hold; keep them in /errors for review.
                    self.db.execute("UPDATE outbox SET status='failed' WHERE status='pending'")
                    for key in ('held','approved','cancelled'):
                        self.db.execute('UPDATE templates SET payload=? WHERE k=?',(json.dumps({'text':DEFAULTS[key]},ensure_ascii=False),key))
                    if self.owner:self.send('Обновление 1.1: временные брони сняты, заявки и подтверждённые оплаты сохранены. Шаблоны выбора времени, счёта и отмены обновлены. Проверь собственные прайс и условия: в них не должно оставаться обещания временной брони. Старые отложенные отправки доступны в /errors.')
                self.set('schema_version','1.1')
            for k,v in DEFAULTS.items(): self.db.execute('INSERT OR IGNORE INTO templates VALUES(?,?)',(k,json.dumps({'text':v},ensure_ascii=False)))
            for k,v in {'enabled':'0','triggers':json.dumps(DEFAULT_TRIGGERS,ensure_ascii=False)}.items(): self.db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(k,v))
            # Do not replay a send whose response may have been lost on a restart.
            self.db.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'")
        self.migrate_orders(path)
    def migrate_orders(self,path):
        if self.setting('schema_version')=='1.2':return
        if self.db.execute('SELECT 1 FROM bookings LIMIT 1').fetchone() or self.db.execute("SELECT 1 FROM templates WHERE k='payment' AND payload!=?",(json.dumps({'text':''},ensure_ascii=False),)).fetchone():
            backup=sqlite3.connect(str(path)+'.before-1.2.sqlite3')
            try:self.db.backup(backup)
            finally:backup.close()
        with self.db:
            existing={r[1] for r in self.db.execute('PRAGMA table_info(bookings)')}
            for name,decl in [('cart',"TEXT DEFAULT '{}'"),('cart_ready','INTEGER DEFAULT 0'),('post_received','INTEGER DEFAULT 0'),('receipt_ack','INTEGER DEFAULT 0'),('revision','INTEGER DEFAULT 0'),('manual_price','INTEGER DEFAULT 0')]:
                if name not in existing:self.db.execute('ALTER TABLE bookings ADD COLUMN '+name+' '+decl)
            self.db.execute('CREATE TABLE IF NOT EXISTS catalog(k TEXT PRIMARY KEY,name TEXT,price INTEGER)')
            for key,name,price in [('base','Публикация поста',400),('pin3','Закреп 3 дня',300),('pin7','Закреп 7 дней',500),('stories','Репост в сторис',500)]:
                self.db.execute('INSERT OR IGNORE INTO catalog VALUES(?,?,?)',(key,name,price))
            self.db.execute('CREATE TABLE IF NOT EXISTS message_groups(conn TEXT,gid TEXT,bid INTEGER,kind TEXT,last_at REAL,PRIMARY KEY(conn,gid))')
            self.db.execute('CREATE TABLE IF NOT EXISTS received_messages(conn TEXT,mid INTEGER,bid INTEGER,PRIMARY KEY(conn,mid))')
            for row in self.db.execute('SELECT id,price,conn,chat FROM bookings').fetchall():
                seen=bool(row['price']) or bool(self.db.execute("SELECT 1 FROM cooldown WHERE conn=? AND chat=? AND kind='post'",(row['conn'],row['chat'])).fetchone())
                self.db.execute('UPDATE bookings SET cart=?,cart_ready=1,post_received=?,manual_price=? WHERE id=?',(json.dumps(self.new_cart(),ensure_ascii=False),int(seen),int(bool(row['price'])),row['id']))
            old_defaults={
                'welcome':'Здравствуйте! Подскажу свободное время и условия размещения. Что вас интересует?',
                'post':'Объявление получила, передала на проверку. Стоимость и размещение подтвердит администратор.',
                'held':'Вы выбрали {slot}. Пришлите объявление для проверки. Администратор подтвердит стоимость и отправит реквизиты. Слот закрепляется только после подтверждения оплаты; пока он остаётся свободным.',
            }
            for key,old in old_defaults.items():
                row=self.db.execute('SELECT payload FROM templates WHERE k=?',(key,)).fetchone()
                if row and json.loads(row[0])=={'text':old}:
                    self.db.execute('UPDATE templates SET payload=? WHERE k=?',(json.dumps({'text':DEFAULTS[key]},ensure_ascii=False),key))
            self.set('schema_version','1.2')
    def setting(self,k): return self.db.execute('SELECT v FROM settings WHERE k=?',(k,)).fetchone()[0]
    def set(self,k,v): self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,str(v)))
    def enqueue(self,method,payload,scope=''):
        self.db.execute('INSERT INTO outbox(method,payload,scope,automatic) VALUES(?,?,?,?)',(method,json.dumps(payload,ensure_ascii=False),scope,int(self.automatic and bool(scope))))
    def send(self,text,chat=None,conn='',markup=None):
        text=text.encode('utf-16-le')[:7600].decode('utf-16-le',errors='ignore')
        payload={'chat_id':chat or self.owner,'text':text,'link_preview_options':{'is_disabled':True}}
        if conn: payload['business_connection_id']=conn
        if markup: payload['reply_markup']=markup
        self.enqueue('sendMessage',payload,conn)
    def template(self,key,chat,conn='',markup=None,append='',**values):
        p=json.loads(self.db.execute('SELECT payload FROM templates WHERE k=?',(key,)).fetchone()[0])
        if not p.get('text') and not p.get('file_id'): return False
        # Formatting entities retained when no variable substitution is used.
        text=p.get('text','');changed=False
        for k,v in values.items():
            new=text.replace('{'+k+'}',str(v));changed|=new!=text;text=new
        text+=append
        payload={'chat_id':chat}
        if markup:payload['reply_markup']=markup
        if conn: payload['business_connection_id']=conn
        if p.get('file_id'):
            method='sendPhoto' if p['media']=='photo' else 'sendDocument'
            payload[p['media']]=p['file_id'];payload['caption']=text
            if not changed and p.get('entities'):payload['caption_entities']=p['entities']
        else:
            method='sendMessage';payload['text']=text
            if not changed and p.get('entities'):payload['entities']=p['entities']
            payload['link_preview_options']={'is_disabled':True}
        self.enqueue(method,payload,conn);return True
    def active(self,conn):
        r=self.db.execute('SELECT active FROM connections WHERE id=?',(conn,)).fetchone()
        return bool(r and r[0])
    def paused(self,conn,chat):
        r=self.db.execute('SELECT until FROM pauses WHERE conn=? AND chat=?',(conn,chat)).fetchone()
        return bool(r and r[0]>self.clock())
    def allowed(self,conn,chat): return self.active(conn) and self.setting('enabled')=='1' and not self.paused(conn,chat)
    def booking(self,bid):
        return self.db.execute('SELECT b.*,s.at FROM bookings b LEFT JOIN slots s ON s.id=b.slot WHERE b.id=?',(bid,)).fetchone()
    def current(self,conn,chat):
        return self.db.execute("SELECT b.*,s.at FROM bookings b LEFT JOIN slots s ON s.id=b.slot WHERE conn=? AND chat=? AND b.status IN ('review','payment','checking','expired')",(conn,chat)).fetchone()
    def notify(self,b):
        tag=f"{b['id']}_{b['revision']}"
        rows=[]
        if b['status']=='review':
            rows.append([('Пост подходит → реквизиты','a:approve:'+tag)])
            rows.append([('Изменить итог','a:price:'+tag)])
        if b['status'] in ('payment','checking'):
            rows.append([('Подтвердить оплату',f'a:paidask:{b["id"]}')])
        rows.extend([[('Отменить заявку',f'a:cancelask:{b["id"]}')],
                     [('Пауза в чате',f'a:pause:{b["id"]}'),('Возобновить',f'a:resume:{b["id"]}')]])
        self.send(f"Заявка №{b['id']} · {b['name']} (ID {b['chat']})\n{stamp(b['at'])}\nСтатус: {STATUS[b['status']]}\nПост: {'получен' if b['post_received'] else 'ожидаем'}\nВыбор услуг: {'готов' if b['cart_ready'] else 'не завершён'}\n\n"+self.order_summary(b),markup=buttons(rows))
    def list_requests(self,offset=0):
        offset=max(0,int(offset))
        rows=self.db.execute("SELECT id FROM bookings WHERE status IN ('review','payment','checking','expired') ORDER BY id LIMIT 9 OFFSET ?",(offset,)).fetchall()
        if not rows:self.send('Неподтверждённых заявок нет на этой странице. Оплаченные — в «Расписание».');return
        for row in rows[:8]:self.notify(self.booking(row[0]))
        nav=[]
        if offset:nav.append(('← Назад',f'a:requests:{max(0,offset-8)}'))
        if len(rows)>8:nav.append(('Следующие заявки →',f'a:requests:{offset+8}'))
        if nav:self.send('Страницы заявок:',markup=buttons([nav]))
    def list_slots(self,chat,conn='',offset=0,admin=False):
        offset=max(0,min(offset,10000))
        query="SELECT * FROM slots WHERE at>?"+('' if admin else " AND status='free'")+" ORDER BY at LIMIT 9 OFFSET ?"
        rows=self.db.execute(query,(self.clock(),offset)).fetchall()
        if not rows:
            if admin:self.send('Слотов пока нет. Нажми «Добавить слоты».')
            else:self.template('empty',chat,conn)
            return
        kb=[]
        for r in rows[:8]:
            label=stamp(r['at'])+((' · '+STATUS[r['status']]) if admin else '')
            kb.append([(label,f'{"a:slot" if admin else "c:slot"}:{r["id"]}')])
        nav=[]
        if offset:nav.append(('← Раньше',f'{"a" if admin else "c"}:page:{max(0,offset-8)}'))
        if len(rows)>8:nav.append(('Позже →',f'{"a" if admin else "c"}:page:{offset+8}'))
        if nav:kb.append(nav)
        if admin:self.send('Расписание · московское время.',chat,conn,buttons(kb))
        else:self.template('slots',chat,conn,markup=buttons(kb))
    def reserve(self,slot,conn,chat,name):
        b=self.ensure_order(conn,chat,name)
        if b['status']!='review':self.send('Счёт уже отправлен. Для изменения времени напишите администратору.',chat,conn);return
        row=self.db.execute("SELECT * FROM slots WHERE id=? AND status='free' AND at>?",(slot,self.clock())).fetchone()
        if not row:self.list_slots(chat,conn);return
        if b['slot']==slot:return
        self.db.execute('UPDATE bookings SET slot=?,revision=revision+1 WHERE id=?',(slot,b['id']))
        b=self.booking(b['id'])
        markup=self.next_buttons(b)
        self.template('held',chat,conn,slot=stamp(row['at']),markup=markup)
        self.notify(b)
    def cancel(self,b):
        if b['status']=='cancelled':return
        self.db.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(b['id'],))
        if b['status']=='confirmed':
            self.db.execute("UPDATE slots SET status='free' WHERE id=? AND status='booked' AND NOT EXISTS (SELECT 1 FROM bookings WHERE slot=? AND status='confirmed')",(b['slot'],b['slot']))
        self.template('cancelled',b['chat'],b['conn'],slot=stamp(b['at']))
        self.send('Заявка отменена.'+(' Слот освобождён.' if b['status']=='confirmed' else ''))
    def admin_callback(self,action,value):
        if action=='catalog':self.catalog_edit(value);return
        if action=='requests':self.list_requests(int(value));return
        if action=='page':self.list_slots(self.owner,offset=int(value),admin=True);return
        if action=='template':
            if value not in DEFAULTS:return
            self.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)',(self.owner,'template:'+value))
            if value=='pricing':
                self.send('Цены задаются в «Цены и дополнения». Чтобы добавить свой текст вокруг автоматического прайса, вставь в шаблон {catalog}. Старый текст без {catalog} хранится, но клиентам вместо него показываются актуальные цены.');
            self.send('Пришли новый шаблон «'+LABELS[value]+'»: текст или одно фото/документ с подписью. Можно переслать готовое сообщение. /cancel — отменить.\nПеременные: {slot}, {price}; для прайса — {catalog}. В реквизитах переменные не нужны.');return
        if action=='triggers':
            if value not in DEFAULT_TRIGGERS:return
            self.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)',(self.owner,'triggers:'+value))
            self.send('Пришли фразы для темы «'+LABELS[value]+'», по одной на строке. Сейчас:\n'+'\n'.join(json.loads(self.setting('triggers'))[value]));return
        if action=='enable':
            missing=[LABELS[k] for k in ('payment','conditions') if not json.loads(self.db.execute('SELECT payload FROM templates WHERE k=?',(k,)).fetchone()[0]).get('text') and not json.loads(self.db.execute('SELECT payload FROM templates WHERE k=?',(k,)).fetchone()[0]).get('file_id')]
            if missing:self.send('Сначала заполни шаблоны: '+', '.join(missing));return
            if not self.db.execute('SELECT 1 FROM connections WHERE active=1').fetchone():self.send('Сначала подключи этого бота в Telegram → Для бизнеса → Чат-боты с правом ответа.');return
            self.set('enabled','1');self.send('Автоответы включены для чатов, доступных боту в настройках Telegram.');return
        if action=='disable':self.set('enabled','0');self.send('Автоответы выключены. Управление расписанием доступно.');return
        if action=='slot':
            r=self.db.execute('SELECT * FROM slots WHERE id=?',(int(value),)).fetchone()
            if not r:return
            b=self.db.execute("SELECT id FROM bookings WHERE slot=? AND status='confirmed' ORDER BY id DESC LIMIT 1",(r['id'],)).fetchone()
            if r['status'] in ('held','booked') and b:self.notify(self.booking(b[0]));return
            self.send(stamp(r['at'])+' · '+STATUS[r['status']],markup=buttons([[('Занять вручную',f'a:block:{r["id"]}'),('Сделать свободным',f'a:free:{r["id"]}')]]));return
        if action in ('block','free'):
            self.db.execute("UPDATE slots SET status=? WHERE id=? AND status IN ('free','blocked')",('blocked' if action=='block' else 'free',int(value)))
            self.send('Готово.');return
        parts=value.split('_');b=self.booking(int(parts[0]))
        if not b:return
        if action in ('pause','resume'):
            self.db.execute('INSERT OR REPLACE INTO pauses VALUES(?,?,?)',(b['conn'],b['chat'],self.clock()+10*365*86400 if action=='pause' else 0))
            self.send('Автоответы в этом чате '+('приостановлены.' if action=='pause' else 'возобновлены.'));return
        if b['status']=='cancelled':self.send('Эта бронь уже отменена.');return
        if action in ('price','approve'):
            if b['status']!='review':self.send('Счёт уже отправлен или оплата подтверждена.');return
            if len(parts)!=2 or int(parts[1])!=b['revision']:
                self.send('Заявка изменилась. Проверь обновлённую карточку.');self.notify(b);return
            if action=='approve':self.issue_invoice(b);return
            self.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)',(self.owner,'price:'+value))
            self.send('Пришли итоговую стоимость числом, например 500. Сумма сохранится в карточке; реквизиты отправятся только по кнопке «Пост подходит → реквизиты». /cancel — отменить.');return
        if action=='paidask':
            self.send('Подтвердить, что деньги по заявке №'+value+' действительно поступили?',markup=buttons([[('Да, деньги поступили',f'a:paid:{value}')]]));return
        if action=='paid':
            if b['status']=='confirmed':self.send('Оплата уже подтверждена.');return
            if b['status'] not in ('payment','checking') or not b['price']:self.send('Сначала проверь пост и отправь счёт через карточку заявки.');return
            if not b['at'] or b['at']<=self.clock():self.send('Время публикации уже прошло. Согласуй новый слот.');return
            changed=self.db.execute("UPDATE slots SET status='booked' WHERE id=? AND status='free' AND NOT EXISTS (SELECT 1 FROM bookings WHERE slot=? AND status='confirmed')",(b['slot'],b['slot'])).rowcount
            if not changed:self.send('Этот слот уже занят. Оплата по этой заявке не подтверждена в боте. Проверь порядок поступления денег и согласуй другое время с мастером.');return
            self.db.execute("UPDATE bookings SET status='confirmed' WHERE id=?",(b['id'],))
            self.template('confirmed',b['chat'],b['conn'],slot=stamp(b['at']),price=b['price']);self.send('Оплата подтверждена, время занято. Пост в канал публикуешь ты.');return
        if action=='cancelask':self.send('Отменить заявку №'+value+'?',markup=buttons([[('Да, отменить',f'a:cancel:{value}')]]));return
        if action=='cancel':self.cancel(b);return
        if action=='extend':self.send('Временной брони больше нет. Слот закрепляется после подтверждения оплаты.');return
    def admin_message(self,m):
        text=m.get('text','').strip()
        if text in ('/cancel','Отмена'):
            self.db.execute('DELETE FROM sessions WHERE chat=?',(self.owner,));self.send('Действие отменено.',markup=MENU);return
        commands=('/start','/help','/status','/id','Расписание','Добавить слоты','Заявки','Шаблоны','Цены и дополнения','Автоответы','Помощь')
        if text in commands:self.db.execute('DELETE FROM sessions WHERE chat=?',(self.owner,))
        mode=self.db.execute('SELECT mode FROM sessions WHERE chat=?',(self.owner,)).fetchone()
        if mode:
            mode=mode[0]
            if mode=='slots':
                parsed=[]
                try:
                    for line in text.splitlines():
                        parts=line.replace(',',' ').split()
                        if len(parts)<2:raise ValueError()
                        for hour in parts[1:]:
                            ts=datetime.strptime(parts[0]+' '+hour,'%d.%m.%Y %H:%M').replace(tzinfo=TZ).timestamp()
                            if ts<=self.clock():raise ValueError()
                            parsed.append(ts)
                    if not parsed or len(parsed)>300:raise ValueError()
                except ValueError:self.send('Нужны будущие даты и время по Москве:\n15.09.2026 12:00 14:00 16:00\n16.09.2026 11:00 13:00\nДо 300 слотов за раз.');return
                for ts in parsed:self.db.execute('INSERT OR IGNORE INTO slots(at) VALUES(?)',(ts,))
                self.send('Слоты добавлены. Уже существующие брони сохранены.')
            elif mode.startswith('template:'):
                key=mode.split(':',1)[1];body=m.get('text') or m.get('caption') or ''
                if m.get('media_group_id'):self.send('Шаблон — одно сообщение, без альбома.');return
                p={'text':body,'entities':m.get('entities',m.get('caption_entities',[]))}
                if m.get('photo'):p.update(media='photo',file_id=m['photo'][-1]['file_id'])
                elif m.get('document'):p.update(media='document',file_id=m['document']['file_id'])
                elif not body:self.send('Пришли текст, фото или документ.');return
                if len(body.encode('utf-16-le'))//2>3000 or (p.get('file_id') and len(body.encode('utf-16-le'))//2>1024):self.send('Сократи шаблон: текст до 3000 символов, подпись к файлу до 1024.');return
                self.db.execute('UPDATE templates SET payload=? WHERE k=?',(json.dumps(p,ensure_ascii=False),key));self.send('Шаблон сохранён.');self.template(key,self.owner,slot='15.09.2026 в 12:00',price='400',catalog=self.catalog_text())
            elif mode.startswith('triggers:'):
                values=[norm(x) for x in text.splitlines() if norm(x)]
                if not values or len(values)>30:self.send('От 1 до 30 фраз, каждая с новой строки.');return
                d=json.loads(self.setting('triggers'));d[mode.split(':')[1]]=values;self.set('triggers',json.dumps(d,ensure_ascii=False));self.send('Фразы сохранены.')
            elif mode.startswith('catalog:'):
                if not self.save_catalog(mode.split(':')[1],text):return
            elif mode.startswith('price:'):
                if not re.fullmatch(r'\d{1,6}',text) or int(text)<=0:self.send('Пришли положительную сумму числом, например 400.');return
                parts=mode.split(':')[1].split('_');b=self.booking(int(parts[0]))
                if not b or b['status']!='review' or len(parts)!=2 or b['revision']!=int(parts[1]):
                    self.send('Заявка изменилась. Открой её карточку заново.');self.db.execute('DELETE FROM sessions WHERE chat=?',(self.owner,));return
                self.db.execute('UPDATE bookings SET price=?,manual_price=1,revision=revision+1 WHERE id=?',(text,b['id']))
                self.send('Итог сохранён. Проверь карточку и нажми отправку реквизитов.');self.notify(self.booking(b['id']))
            self.db.execute('DELETE FROM sessions WHERE chat=?',(self.owner,));return
        if text=='Добавить слоты':self.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)',(self.owner,'slots'));self.send('Пришли даты и свободное время по Москве:\n15.09.2026 12:00 14:00 16:00\n16.09.2026 11:00 13:00\nУказывай нужный год. /cancel — отменить.');return
        if text=='Расписание':self.list_slots(self.owner,admin=True);return
        if text=='Заявки':self.list_requests();return
        if text=='Цены и дополнения':self.show_catalog_admin();return
        if text=='Шаблоны':
            self.send('Выбери шаблон для изменения:',markup=buttons([[(v,'a:template:'+k)] for k,v in LABELS.items()]))
            self.send('Фразы, по которым бот выбирает ответ:',markup=buttons([[(LABELS[k],'a:triggers:'+k)] for k in DEFAULT_TRIGGERS]));return
        if text=='Автоответы':self.send('Сейчас: '+('включены' if self.setting('enabled')=='1' else 'выключены'),markup=buttons([[('Включить','a:enable:0'),('Выключить','a:disable:0')]]));return
        if text.startswith('/hold'):
            self.send('Временная бронь отключена. Слот закрепляется только после подтверждения оплаты.');return
        if text.startswith('/resume '):
            v=text.split()[-1]
            if v.isdigit():self.db.execute('DELETE FROM pauses WHERE chat=?',(int(v),));self.send('Пауза снята для чата '+v+'.')
            return
        if text in ('/id','/status'):
            failed=self.db.execute("SELECT count(*) FROM outbox WHERE status IN ('failed','uncertain')").fetchone()[0]
            self.send(f'Business-бот {VERSION}. ID владельца: {self.owner}\nАвтоответы: {self.setting("enabled")}\nБронь: только после подтверждения оплаты. Время: Москва. Webhook, без таймеров брони.\nНеотправленные/неподтверждённые отправки: {failed}\n/errors — показать последние.');return
        if text=='/errors':
            for r in self.db.execute("SELECT id,method,payload,status FROM outbox WHERE status IN ('failed','uncertain') ORDER BY id DESC LIMIT 10").fetchall():
                payload=json.loads(r['payload']);self.send(f"Отправка №{r['id']} · {r['status']} · чат {payload.get('chat_id')}\n{(payload.get('text') or payload.get('caption') or r['method'])[:1200]}\nПроверь переписку. При необходимости отправь вручную.")
            return
        self.send('Это отдельный помощник по размещениям.\n1. Добавь слоты.\n2. Проверь «Цены и дополнения», заполни условия и реквизиты в «Шаблоны».\n3. Подключи бота к выбранным чатам через Telegram Business.\n4. Включи автоответы.\n\nВыбор слота создаёт заявку, время остаётся свободным. Мастер выбирает дополнения, бот считает итог. Ты проверяешь пост, отправляешь реквизиты кнопкой и подтверждаешь поступление денег. Только после этого слот занят. Если заявок несколько, подтверждай ту, по которой деньги поступили первыми.\nТвой ручной ответ приостанавливает бота в чате на 30 минут. /resume ID — снять паузу.\n/errors — ошибки доставки.\nПосты в канал публикуешь ты.',markup=MENU)
    def new_cart(self):
        return {'items':{r['k']:{'name':r['name'],'price':r['price']} for r in self.db.execute('SELECT * FROM catalog')},'pin':'','stories':False}
    def ensure_order(self,conn,chat,name='Мастер'):
        old=self.current(conn,chat)
        if old:return old
        cart=self.new_cart()
        cur=self.db.execute("INSERT INTO bookings(slot,conn,chat,name,status,deadline,price,cart) VALUES(NULL,?,?,?,'review',0,?,?)",(conn,chat,name[:150],str(cart['items']['base']['price']),json.dumps(cart,ensure_ascii=False)))
        return self.booking(cur.lastrowid)
    def selected_items(self,b):
        cart=json.loads(b['cart']);keys=['base']
        if cart.get('pin'):keys.append(cart['pin'])
        if cart.get('stories'):keys.append('stories')
        return [cart['items'][key] for key in keys]
    def order_summary(self,b):
        lines=[f"{x['name']} — {x['price']} ₽" for x in self.selected_items(b)]
        total=b['price'] or str(sum(x['price'] for x in self.selected_items(b)))
        lines.append(('Итог с учётом индивидуальной цены: ' if b['manual_price'] else 'Итого: ')+total+' ₽')
        return '\n'.join(lines)
    def catalog_text(self):
        data={r['k']:r for r in self.db.execute('SELECT * FROM catalog')}
        lines=[f"{data['base']['name']} — {data['base']['price']} ₽",'', 'Дополнительно:']
        lines.extend(f"– {data[k]['name']} — {data[k]['price']} ₽" for k in ('pin3','pin7','stories'))
        return '\n'.join(lines)
    def show_catalog_admin(self):
        rows=[[(f"{r['name']} · {r['price']} ₽",'a:catalog:'+r['k'])] for r in self.db.execute("SELECT * FROM catalog ORDER BY CASE k WHEN 'base' THEN 0 WHEN 'pin3' THEN 1 WHEN 'pin7' THEN 2 ELSE 3 END")]
        self.send('Цены и дополнения. Выбери услугу, чтобы изменить название и цену.\n\n'+self.catalog_text()+'\n\nИзменения применяются к новым заказам. Уже показанные мастеру цены сохраняются; их можно поправить вручную в карточке.',markup=buttons(rows))
    def catalog_edit(self,key):
        r=self.db.execute('SELECT * FROM catalog WHERE k=?',(key,)).fetchone()
        if not r:return
        self.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)',(self.owner,'catalog:'+key))
        self.send('Пришли название и цену двумя строками, например:\n'+r['name']+'\n'+str(r['price'])+'\n\nНазвание до 32 символов, цена — целое число рублей. /cancel — отменить.')
    def save_catalog(self,key,text):
        lines=[x.strip() for x in text.strip().splitlines()]
        if len(lines)!=2 or not lines[0] or len(lines[0])>32 or not re.fullmatch(r'\d{1,6}',lines[1]) or int(lines[1])<=0:
            self.send('Нужны две строки: название до 32 символов и положительная цена числом, например:\nПубликация поста\n400');return False
        self.db.execute('UPDATE catalog SET name=?,price=? WHERE k=?',(lines[0],int(lines[1]),key))
        self.send('Название и цена сохранены. Новые заказы будут использовать их.');self.show_catalog_admin();return True
    def menu_buttons(self):
        return buttons([[('Стоимость и дополнения','c:pricing:0')],[('Свободное время','c:page:0'),('Условия','c:conditions:0')]])
    def next_buttons(self,b):
        if b['status'] in ('payment','checking'):return buttons([[('Я оплатил(а)',f'c:paid:{b["id"]}')]])
        if not b['cart_ready']:return buttons([[('Выбрать дополнения',f'c:cart:{b["id"]}')]])
        if not b['slot']:return buttons([[('Выбрать время','c:page:0')]])
        return buttons([[('Изменить дополнения',f'c:cart:{b["id"]}')],[('Изменить время','c:page:0')]])
    def show_pricing(self,conn,chat,prefix=''):
        # A legacy free-text price list remains editable but cannot override catalog prices.
        p=json.loads(self.db.execute("SELECT payload FROM templates WHERE k='pricing'").fetchone()[0])
        body=p.get('text','')
        body=body.replace('{catalog}',self.catalog_text()) if '{catalog}' in body else self.catalog_text()
        self.send(prefix+body,chat,conn,buttons([[('Выбрать размещение','c:cart:0')],[('Условия','c:conditions:0')]]))
    def cart_view(self,b,message=None):
        cart=json.loads(b['cart']);tag=f"{b['id']}_{b['revision']}"
        rows=[]
        for key in ('pin3','pin7','stories'):
            item=cart['items'][key];chosen=cart.get('stories') if key=='stories' else cart.get('pin')==key
            rows.append([(('✓ ' if chosen else '')+f"{item['name']} · +{item['price']} ₽",f'c:add:{tag}_{key}')])
        rows.append([('Продолжить',f'c:done:{tag}')])
        text=self.order_summary(b)+'\n\nДобавьте закреп и/или сторис. Без дополнений просто нажмите «Продолжить». Повторное нажатие снимает выбор.'
        markup=buttons(rows)
        if message and message.get('message_id') and message.get('text'):
            self.enqueue('editMessageText',{'business_connection_id':b['conn'],'chat_id':b['chat'],'message_id':message['message_id'],'text':text,'reply_markup':markup,'link_preview_options':{'is_disabled':True}},b['conn'])
        else:self.send(text,b['chat'],b['conn'],markup)
    def issue_invoice(self,b):
        if b['status']!='review':self.send('Счёт уже отправлен.');return
        if not b['post_received']:self.send('Сначала нужен пост мастера.');return
        if not b['cart_ready']:self.send('Мастер ещё не завершил выбор дополнений.');return
        if not b['at'] or b['at']<=self.clock():self.send('Сначала нужно выбрать будущее время.');return
        if self.db.execute('SELECT status FROM slots WHERE id=?',(b['slot'],)).fetchone()[0]!='free':self.send('Слот уже занят. Согласуй другое время; реквизиты не отправлены.');return
        recent=self.db.execute("SELECT 1 FROM message_groups WHERE bid=? AND kind='post' AND last_at>?",(b['id'],self.clock()-2)).fetchone()
        if recent:self.send('Фотографии альбома ещё могут поступать. Проверь весь альбом и нажми кнопку снова через несколько секунд.');return
        pay=json.loads(self.db.execute("SELECT payload FROM templates WHERE k='payment'").fetchone()[0])
        if not pay.get('text') and not pay.get('file_id'):self.send('Сначала заполни шаблон «Реквизиты».');return
        self.db.execute("UPDATE bookings SET status='payment',receipt_ack=0 WHERE id=?",(b['id'],))
        self.template('approved',b['chat'],b['conn'],slot=stamp(b['at']),price=b['price'],append='\n\n'+self.order_summary(b))
        self.template('payment',b['chat'],b['conn'],markup=buttons([[('Я оплатил(а)',f'c:paid:{b["id"]}')]]))
        self.send('Счёт и реквизиты поставлены на отправку. Слот пока свободен.');self.notify(self.booking(b['id']))
    def mark_receipt(self,b):
        if b['status'] not in ('payment','checking'):return
        if not b['receipt_ack']:
            self.db.execute("UPDATE bookings SET status='checking',receipt_ack=1 WHERE id=?",(b['id'],))
            self.template('paid_claim',b['chat'],b['conn'])
            self.send('Мастер сообщил об оплате или прислал возможный чек. Проверь поступление денег.');self.notify(self.booking(b['id']))
    def client_callback(self,act,val,conn,chat,name,message):
        if act=='page':self.list_slots(chat,conn,int(val));return
        if act=='slot':self.reserve(int(val),conn,chat,name);return
        if act=='pricing':self.show_pricing(conn,chat);return
        if act=='conditions':self.template('conditions',chat,conn,markup=self.menu_buttons());return
        if act=='paid':
            b=self.booking(int(val))
            if b and b['conn']==conn and b['chat']==chat:self.mark_receipt(b)
            return
        if act=='cart':
            b=self.current(conn,chat)
            if val!='0' and (not b or b['id']!=int(val)):self.send('Эта заявка уже завершена. Для нового заказа нажмите «Стоимость».',chat,conn);return
            b=b or self.ensure_order(conn,chat,name)
            if b['status']!='review':self.send('Счёт уже отправлен. Изменения согласуйте с администратором.',chat,conn);return
            if b['cart_ready']:
                self.db.execute('UPDATE bookings SET cart_ready=0,revision=revision+1 WHERE id=?',(b['id'],));b=self.booking(b['id'])
            self.cart_view(b);return
        if act not in ('add','done'):return
        parts=val.split('_')
        if len(parts) not in (2,3) or not parts[0].isdigit() or not parts[1].isdigit():return
        b=self.booking(int(parts[0]))
        if not b or b['conn']!=conn or b['chat']!=chat or b['status']!='review':return
        if b['revision']!=int(parts[1]) or b['cart_ready']:return
        if act=='add':
            if len(parts)!=3 or parts[2] not in ('pin3','pin7','stories'):return
            key=parts[2];cart=json.loads(b['cart'])
            if key=='stories':cart['stories']=not cart.get('stories')
            else:cart['pin']='' if cart.get('pin')==key else key
            keys=['base']+([cart['pin']] if cart['pin'] else [])+(['stories'] if cart['stories'] else [])
            total=sum(cart['items'][k]['price'] for k in keys)
            self.db.execute('UPDATE bookings SET cart=?,price=?,manual_price=0,revision=revision+1 WHERE id=?',(json.dumps(cart,ensure_ascii=False),str(total),b['id']))
            self.cart_view(self.booking(b['id']),message);return
        self.db.execute('UPDATE bookings SET cart_ready=1,revision=revision+1 WHERE id=?',(b['id'],))
        b=self.booking(b['id']);self.notify(b)
        if not b['slot']:self.list_slots(chat,conn)
        elif not b['post_received']:self.send(self.order_summary(b)+'\n\nПришлите, пожалуйста, объявление с фотографиями для проверки.',chat,conn)
        else:self.send(self.order_summary(b)+'\n\nВыбор сохранён. После проверки объявления пришлю реквизиты.',chat,conn)
    def forward_to_owner(self,b,m,kind):
        label=f"Заявка №{b['id']} · "+('возможный чек' if kind=='receipt' else 'сообщение мастера')
        text=m.get('text') or m.get('caption') or ''
        if text:self.send(label+'\n'+text[:3300])
        if m.get('photo'):self.enqueue('sendPhoto',{'chat_id':self.owner,'photo':m['photo'][-1]['file_id'],'caption':label})
        elif m.get('document'):self.enqueue('sendDocument',{'chat_id':self.owner,'document':m['document']['file_id'],'caption':label})
        elif m.get('video'):self.enqueue('sendVideo',{'chat_id':self.owner,'video':m['video']['file_id'],'caption':label})
    def business_message(self,m):
        conn=m.get('business_connection_id','');chat=m.get('chat',{}).get('id');who=m.get('from',{}).get('id')
        if not chat or m.get('chat',{}).get('type')!='private' or not self.active(conn):return
        if who==self.owner:
            if not m.get('sender_business_bot') and not m.get('is_from_offline'):
                self.db.execute('INSERT INTO pauses VALUES(?,?,?) ON CONFLICT(conn,chat) DO UPDATE SET until=MAX(until,excluded.until)',(conn,chat,self.clock()+1800))
            return
        if m.get('from',{}).get('is_bot') or m.get('sender_business_bot') or m.get('is_from_offline') or not self.allowed(conn,chat):return
        mid=m.get('message_id')
        if mid is not None and self.db.execute('SELECT 1 FROM received_messages WHERE conn=? AND mid=?',(conn,mid)).fetchone():return
        text=m.get('text') or m.get('caption') or '';n=norm(text);b=self.current(conn,chat)
        media=bool(m.get('photo') or m.get('document') or m.get('video'))
        gid=m.get('media_group_id');group=self.db.execute('SELECT * FROM message_groups WHERE conn=? AND gid=?',(conn,gid)).fetchone() if gid else None
        if group:
            b=self.booking(group['bid']);kind=group['kind']
        else:kind='receipt' if b and b['status'] in ('payment','checking') else 'post'
        triggers=json.loads(self.setting('triggers'))
        kinds=[k for k,phrases in triggers.items() if any((' '+norm(p)+' ') in (' '+n+' ') for p in phrases)]
        paid= 'paid_claim' in kinds or n in ('оплачено','готово с оплатой','оплата готово')
        if b and b['status'] in ('payment','checking') and (media or paid) and kind=='receipt':
            if gid:self.db.execute('INSERT INTO message_groups VALUES(?,?,?,?,?) ON CONFLICT(conn,gid) DO UPDATE SET last_at=excluded.last_at',(conn,gid,b['id'],'receipt',self.clock()))
            if mid is not None:self.db.execute('INSERT OR IGNORE INTO received_messages VALUES(?,?,?)',(conn,mid,b['id']))
            self.forward_to_owner(b,m,'receipt');self.mark_receipt(b);return
        if not media and len(text)<120 and re.search(r'\b(закреп\w*|сторис)\b',n) and (not b or b['status']=='review'):
            b=b or self.ensure_order(conn,chat,m.get('from',{}).get('first_name','Мастер'))
            if b['cart_ready']:
                self.db.execute('UPDATE bookings SET cart_ready=0,revision=revision+1 WHERE id=?',(b['id'],));b=self.booking(b['id'])
            self.cart_view(b);return
        # Albums are routed by their first item, even if an invoice was sent in between.
        postlike=media or bool(m.get('forward_origin')) or len(text)>=120 or (b and not b['post_received'] and len(text)>=20 and '?' not in text and not kinds)
        if (group and kind=='post') or (postlike and (not b or b['status']=='review')):
            b=b or self.ensure_order(conn,chat,m.get('from',{}).get('first_name','Мастер'))
            if gid:self.db.execute('INSERT INTO message_groups VALUES(?,?,?,?,?) ON CONFLICT(conn,gid) DO UPDATE SET last_at=excluded.last_at',(conn,gid,b['id'],'post',self.clock()))
            if mid is not None:self.db.execute('INSERT OR IGNORE INTO received_messages VALUES(?,?,?)',(conn,mid,b['id']))
            self.forward_to_owner(b,m,'post')
            if not b['post_received']:
                self.db.execute('UPDATE bookings SET post_received=1,revision=revision+1 WHERE id=?',(b['id'],))
                b=self.booking(b['id']);self.template('post',chat,conn,markup=self.next_buttons(b));self.notify(b)
            return
        greeted=bool(re.match(r'^(здравствуйте|добрый день|добрый вечер|доброе утро|привет)(\s|$)',n))
        last=self.db.execute("SELECT at FROM cooldown WHERE conn=? AND chat=? AND kind='hello'",(conn,chat)).fetchone()
        new_hello=greeted and (not last or self.clock()-last[0]>86400)
        if new_hello:
            self.db.execute("INSERT OR REPLACE INTO cooldown VALUES(?,?,'hello',?)",(conn,chat,self.clock()))
        if 'pricing' in kinds:self.show_pricing(conn,chat,'Здравствуйте!\n\n' if new_hello else '');return
        if new_hello:
            self.ensure_order(conn,chat,m.get('from',{}).get('first_name','Мастер'))
            self.template('welcome',chat,conn,markup=self.menu_buttons());return
        if 'slots' in kinds:self.list_slots(chat,conn);return
        if 'conditions' in kinds:self.template('conditions',chat,conn,markup=self.menu_buttons());return
        if b:
            self.forward_to_owner(b,m,'post')
            # Supplementary questions are left to the owner; no repeated post acknowledgement.
    def handle(self,u):
        self.automatic=False
        c=u.get('business_connection')
        if c:
            if c.get('user',{}).get('id')!=self.owner:return
            active=bool(c.get('is_enabled') and (c.get('rights') or {}).get('can_reply',c.get('can_reply',False)))
            if active:
                self.db.execute('UPDATE connections SET active=0')
                self.db.execute('UPDATE bookings SET conn=? WHERE conn!=?',(c['id'],c['id']))
                for table in ('message_groups','received_messages'):
                    self.db.execute('UPDATE OR IGNORE '+table+' SET conn=? WHERE conn!=?',(c['id'],c['id']))
                old_pauses=self.db.execute('SELECT chat,MAX(until) FROM pauses GROUP BY chat').fetchall()
                self.db.execute('DELETE FROM pauses')
                for row in old_pauses:self.db.execute('INSERT INTO pauses VALUES(?,?,?)',(c['id'],row[0],row[1]))
            self.db.execute('INSERT OR REPLACE INTO connections VALUES(?,?)',(c['id'],int(active)))
            self.send('Business-подключение '+('активно. Можно включить автоответы.' if active else 'отключено или нет права ответа.'));return
        q=u.get('callback_query')
        if q:
            self.enqueue('answerCallbackQuery',{'callback_query_id':q['id']})
            m=q.get('message',{});data=q.get('data','').split(':');uid=q.get('from',{}).get('id')
            if len(data)!=3:return
            role,act,val=data;conn=m.get('business_connection_id','');chat=m.get('chat',{}).get('id')
            if role=='a' and not conn and uid==self.owner and chat==self.owner:self.admin_callback(act,val)
            elif role=='c' and conn and uid==chat and uid!=self.owner and self.allowed(conn,chat):
                self.automatic=True
                self.client_callback(act,val,conn,chat,q['from'].get('first_name','Мастер'),m)
            return
        if u.get('business_message'):
            self.automatic=True;self.business_message(u['business_message']);return
        m=u.get('message')
        if not m or m.get('chat',{}).get('type')!='private':return
        uid=m.get('from',{}).get('id')
        if m.get('text') in ('/id','/start') and not self.owner:
            self.send(f'Твой Telegram ID: {uid}. Укажи OWNER_TELEGRAM_ID в Railway, затем перезапусти сервис.',m['chat']['id']);return
        if uid==self.owner and m['chat']['id']==self.owner:self.admin_message(m)
    def receive(self,u):
        if not isinstance(u,dict) or type(u.get('update_id')) is not int:raise ValueError()
        with self.lock,self.db:self.db.execute('INSERT OR IGNORE INTO inbox(id,payload,at) VALUES(?,?,?)',(u['update_id'],json.dumps(u),self.clock()))
        self.event.set()
    def step(self):
        with self.lock:
            row=self.db.execute("SELECT * FROM inbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row:
                try:
                    with self.db:
                        self.handle(json.loads(row['payload']))
                        self.db.execute("UPDATE inbox SET status='done',payload=NULL WHERE id=?",(row['id'],))
                except Exception as exc:
                    with self.db:
                        self.db.execute("UPDATE inbox SET status='failed',payload=NULL WHERE id=?",(row['id'],))
                        if self.owner:self.send('Не удалось обработать событие. Проверь переписку и заявку. Код: '+type(exc).__name__)
            with self.db:
                self.automatic=False
                self.db.execute("DELETE FROM inbox WHERE status='done' AND at<?",(self.clock()-7*86400,))
            out=self.db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if not out or out['after']>self.clock():return bool(row)
            payload=json.loads(out['payload'])
            # Revoking Business permission cancels queued business replies.
            if out['scope'] and (not self.active(out['scope']) or (out['automatic'] and not self.allowed(out['scope'],payload.get('chat_id')))):
                with self.db:self.db.execute("UPDATE outbox SET status='failed' WHERE id=?",(out['id'],))
                return True
            with self.db:self.db.execute("UPDATE outbox SET status='sending' WHERE id=?",(out['id'],))
        try:
            self.sender(out['method'],payload)
        except RateLimit as exc:
            with self.lock,self.db:self.db.execute("UPDATE outbox SET status='pending',after=? WHERE id=?",(self.clock()+max(1,exc.delay),out['id']))
        except Exception:
            with self.lock,self.db:
                self.db.execute("UPDATE outbox SET status='uncertain' WHERE id=?",(out['id'],))
                if out['scope'] and self.owner:self.send('Не удалось подтвердить доставку ответа в чат '+str(payload.get('chat_id'))+'. Проверь переписку; /errors покажет сообщение. Заявка сохранена.')
        else:
            with self.lock,self.db:self.db.execute("UPDATE outbox SET status='sent',payload='{}' WHERE id=?",(out['id'],))
        return True
    def idle_timeout(self):
        # No periodic polling. Wake only for an update, shutdown or a due 429 retry.
        with self.lock:
            if self.db.execute("SELECT 1 FROM inbox WHERE status='pending' LIMIT 1").fetchone():return 0
            pending=self.db.execute("SELECT after FROM outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            return max(0,pending[0]-self.clock()) if pending else None
    def work(self):
        while not self.stopping.is_set():
            self.event.clear()
            try:
                if self.step():continue
                timeout=self.idle_timeout()
            except Exception:
                print('Worker error; check persistent storage.',flush=True);timeout=30
            self.event.wait(timeout)


def main():
    token=os.environ.get('TELEGRAM_BOT_TOKEN','').strip()
    if not token:raise SystemExit('Set TELEGRAM_BOT_TOKEN')
    folder=Path(os.environ.get('STATE_DIR','/data'));folder.mkdir(parents=True,exist_ok=True)
    app=App(str(folder/'business.sqlite3'),os.environ.get('OWNER_TELEGRAM_ID','0'),token)
    secret=hmac.new(token.encode(),b'business-booking-v1',hashlib.sha256).hexdigest()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def setup(self):super().setup();self.connection.settimeout(10)
        def reply(self,code):self.send_response(code);self.end_headers();self.wfile.write(b'OK' if code==200 else b'Unavailable')
        def do_GET(self):self.reply(200 if self.path=='/health' else 404)
        def do_POST(self):
            if self.path!='/telegram' or not hmac.compare_digest(self.headers.get('X-Telegram-Bot-Api-Secret-Token',''),secret):self.reply(403);return
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=2_000_000:raise ValueError()
                u=json.loads(self.rfile.read(n));app.receive(u)
            except (ValueError,UnicodeDecodeError):self.reply(400);return
            except Exception:self.reply(503);return
            self.reply(200)
    server=ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    worker=threading.Thread(target=app.work,daemon=True);worker.start()
    url=os.environ.get('PUBLIC_URL','').strip().rstrip('/')
    if not url and os.environ.get('RAILWAY_PUBLIC_DOMAIN'):url='https://'+os.environ['RAILWAY_PUBLIC_DOMAIN']
    if not url.startswith('https://'):print('Generate public domain (port 8080), then redeploy.',flush=True)
    else:
        try:
            api(token,'setWebhook',{'url':url+'/telegram','secret_token':secret,'allowed_updates':['message','callback_query','business_connection','business_message'],'drop_pending_updates':False,'max_connections':1})
            print('Business bot 1.2: webhook registered.',flush=True)
        except Exception:print('Webhook registration failed. Check token/domain and redeploy.',flush=True)
    import signal
    def stop(*args):app.stopping.set();app.event.set()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    app.stopping.wait();server.shutdown();worker.join(35)
    if not worker.is_alive():app.db.close()
if __name__=='__main__':main()
