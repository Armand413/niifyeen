from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader
from pathlib import Path
import pypdfium2 as pdfium

OUT=Path('output/pdf/NiifYeen_Presentation_Projet.pdf')
pdfmetrics.registerFont(TTFont('Segoe','C:/Windows/Fonts/segoeui.ttf'))
pdfmetrics.registerFont(TTFont('SegoeBold','C:/Windows/Fonts/segoeuib.ttf'))
W,H=595.28,841.89
c=canvas.Canvas(str(OUT),pagesize=(W,H))
c.setTitle('NiifYéén - Présentation du projet')
c.setAuthor('Équipe Argus - École Polytechnique de Ouagadougou')
navy=HexColor('#163247'); teal=HexColor('#117F85'); gray=HexColor('#536575')
styles={
 'body':ParagraphStyle('body',fontName='Segoe',fontSize=10.5,leading=16,textColor=navy),
 'small':ParagraphStyle('small',fontName='Segoe',fontSize=9,leading=13,textColor=gray),
 'heading':ParagraphStyle('heading',fontName='SegoeBold',fontSize=16,leading=21,textColor=navy),
 'white':ParagraphStyle('white',fontName='Segoe',fontSize=11,leading=17,textColor=white),
}
def p(text,y,style='body',x=44,width=507):
 para=Paragraph(text,styles[style]);_,h=para.wrap(width,700);para.drawOn(c,x,y-h);return y-h-12

def base(page,label):
 c.setFillColor(teal);c.rect(0,H-9,W,9,fill=1,stroke=0)
 c.setFont('SegoeBold',9);c.setFillColor(teal);c.drawString(44,798,'NIIFYÉÉN / DOSSIER PROJET')
 c.setFont('Segoe',9);c.setFillColor(gray);c.drawRightString(W-44,798,label)
 c.setStrokeColor(HexColor('#DCE5EB'));c.line(44,49,W-44,49)
 c.setFont('Segoe',8);c.drawString(44,32,'Présentation technique • 2 novembre 2026');c.drawRightString(W-44,32,f'{page} / 3')

def head(n,title,y):
 c.setFillColor(teal);c.setFont('SegoeBold',10);c.drawString(44,y-15,n)
 return p(title,y,'heading',x=72,width=479)

base(1,'VISION & OBJECTIFS')
c.setFont('SegoeBold',39);c.setFillColor(navy);c.drawString(44,728,'NiifYéén')
y=p('Supervision vidéo multi-caméras<br/>et journalisation locale des observations',705,'heading')
y=p('Présentation du prototype pour le Hackathon des Universités 2026',y,'small')
c.setFillColor(navy);c.roundRect(44,503,507,109,12,fill=1,stroke=0)
c.setFillColor(white);c.setFont('SegoeBold',16);c.drawString(62,586,'Équipe Argus')
c.setFont('Segoe',10)
for yy, line in [(565,'ÉCOLE POLYTECHNIQUE DE OUAGADOUGOU'),(546,'COMPAORE Elisé'),(532,'KI Armand'),(518,'OUEDRAOGO Monhamad')]:
 c.drawString(62,yy,line)
y=head('01','Le problème abordé',478)
y=p('Consulter plusieurs caméras séparément complique la lecture des événements et la recherche d’informations dans le temps. Une interface commune peut faciliter la consultation des flux et l’analyse de l’activité enregistrée.',y)
y=head('02','La réponse proposée',y-4)
y=p('NiifYéén réunit une application Python, une interface web en français et une base SQLite locale. Le projet associe affichage vidéo, détection de personnes, suivi de pistes et consultation d’un historique. Il sert de support de démonstration et d’expérimentation technique.',y)
y=head('03','Le cadre du prototype',y-4)
y=p('Les essais annoncés par le porteur concernent ses propres images et un groupe de camarades consentants. Le projet reste expérimental : aucune mesure de précision, de latence ou de fiabilité en conditions réelles n’est revendiquée dans ce dossier.',y)
c.setFillColor(HexColor('#EAF5F5'));c.roundRect(44,77,507,70,9,fill=1,stroke=0)
p('DÉPÔT DU PROJET',132,'small',x=59,width=477)
c.setFont('SegoeBold',11);c.setFillColor(teal);c.drawString(59,99,'https://github.com/Armand413/niifyeen')
c.linkURL('https://github.com/Armand413/niifyeen',(56,92,470,116),relative=0)
c.showPage()

base(2,'SOLUTION & ARCHITECTURE')
y=p('Une architecture locale et modulaire',751,'heading')
y=p('Les fonctions ci-dessous sont décrites à partir du dépôt local consulté. Leur présence dans le code ne constitue pas une validation de leurs performances.',y,'small')
y=head('01','Les composants',y-6)
for name,desc in [
 ('Acquisition et analyse','OpenCV assure la capture. YOLOv8 et ByteTrack sont utilisés pour la détection des personnes et le suivi des pistes dans les images.'),
 ('Serveur applicatif','FastAPI expose les pages et les routes de consultation. Les images sont diffusées en MJPEG et les événements transmis par WebSocket.'),
 ('Interface et historique','Une interface HTML, CSS et JavaScript présente les flux, un plan schématique et les journaux. SQLAlchemy gère la persistance des observations dans SQLite.')]:
 y=p('<b>'+name+'</b><br/>'+desc,y)
y=head('02','Circulation des informations',y-3)
# Simple vector architecture diagram.
for i,(title,sub) in enumerate([('CAMÉRAS','Sources vidéo'),('APPLICATION','Analyse + serveur'),('CONSULTATION','Web + historique')]):
 x=44+i*174;c.setFillColor(HexColor('#EEF3F7'));c.roundRect(x,y-64,159,60,7,fill=1,stroke=0)
 c.setFont('SegoeBold',9);c.setFillColor(teal);c.drawString(x+12,y-25,title)
 c.setFont('Segoe',9);c.setFillColor(navy);c.drawString(x+12,y-43,sub)
 if i<2:
  c.setStrokeColor(teal);c.line(x+161,y-34,x+171,y-34)
y-=86
y=head('03','Fonctions présentes dans le dépôt',y)
y=p('Le code comprend la configuration des caméras, la diffusion vidéo, des filtres de consultation, des mécanismes de migration de la base et des tests automatisés avec composants simulés.',y)
y=p('Des modules de reconnaissance faciale, de recherche par photo et de rapprochement entre caméras sont également présents. Ils reposent notamment sur InsightFace et ONNX Runtime. Le projet ne doit donc pas être présenté comme exclusivement anonyme. Ce dossier en décrit l’existence sans détailler leur mise en œuvre.',y)
y=head('04','Lecture du plan',y-3)
y=p('Le plan est un support visuel schématique. Une liaison entre deux caméras représente une succession d’observations ; elle ne mesure ni le déplacement physique exact ni la position d’une personne entre les caméras.',y)
c.showPage()

base(3,'MATURITÉ & PERSPECTIVES')
y=p('État actuel et prochaines étapes',751,'heading')
y=head('01','Ce qui est disponible',y-8)
y=p('Le dépôt local contient le serveur, les pages web, les fichiers de dépendances, un exemple de configuration des caméras, une documentation de lancement et une suite de tests. La documentation décrit également des fonctions de suppression de données et des limites de conservation.',y)
y=head('02','Ce qui reste à valider',y-4)
y=p('Les tests simulés ne remplacent pas des essais matériels. Il reste à documenter la cadence réelle, la consommation de ressources, la robustesse aux coupures et les erreurs d’analyse. Aucune nouvelle exécution de la suite de tests ni démonstration sur caméras réelles n’a été réalisée pour produire ce PDF.',y)
y=p('Le prototype ne dispose pas encore d’authentification ni de gestion de rôles. La documentation prévoit un usage local ; l’exposition publique nécessiterait un travail distinct sur les accès, la sécurité et la protection des données.',y)
y=head('03','Perspectives de développement',y-4)
y=p('Priorités proposées : fiabiliser les flux et les messages d’erreur ; améliorer l’ergonomie ; formaliser les tests de performance ; renforcer les contrôles d’accès, la conservation et la suppression des données ; faciliter la reproductibilité de l’installation.',y)
y=head('04','Positionnement pour le hackathon',y-4)
y=p('Le projet est présenté dans le contexte du thème « Solutions open sources endogènes ». Son interface en français et son architecture locale constituent des pistes d’adaptation aux usages de l’équipe. La pertinence pour le challenge pré-hackathon doit être vérifiée à partir de son cahier des charges, non analysé dans ce dossier.',y)
y=p('Le dépôt GitHub configuré est indiqué ci-dessous. Son accessibilité publique et la synchronisation avec la version locale n’ont pas été vérifiées. Aucune licence de projet n’a été trouvée à la racine : le statut open source doit être formalisé, en tenant compte des licences des bibliothèques et des modèles.',y,'small')
y=p('<link href="https://github.com/Armand413/niifyeen" color="#117F85">github.com/Armand413/niifyeen</link>',y)
p('Base documentaire : README, fichiers de dépendances et structure du dépôt local consultés le 2 novembre 2026. Ce document présente un prototype, pas un produit certifié ou prêt au déploiement.',y,'small')
c.save()
r=PdfReader(OUT);assert len(r.pages)==3
assert any('https://github.com/Armand413/niifyeen' in str(a.get_object()) for page in r.pages for a in page.get('/Annots',[]))
doc=pdfium.PdfDocument(str(OUT))
for i,page in enumerate(doc):page.render(scale=1.2).to_pil().save(f'tmp/pdfs/page-{i+1}.png')
print(str(OUT.resolve()));print('Pages:',len(r.pages))
