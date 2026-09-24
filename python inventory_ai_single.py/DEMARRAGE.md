# Inventory AI — démarrer et tester dans un vrai restaurant

## 1. Activer la vraie IA (10 min, une seule fois)

1. Crée un compte sur **https://console.anthropic.com**, ajoute un moyen de paiement
   (facturation à l'usage), et fixe une **limite de dépenses mensuelle** (Settings → Limits).
2. **API Keys → Create Key** : copie la clé (`sk-ant-...`). Elle ne s'affiche qu'une fois.
3. Dans ce dossier, copie `.env.example` en `.env` et remplis :
   ```
   ANTHROPIC_API_KEY=sk-ant-...ta-clé...
   INVENTORY_ACCESS_CODE=un-code-que-tu-choisis
   ```
   Ne partage jamais le fichier `.env` (quiconque a la clé peut dépenser sur ton compte).
4. Installe les dépendances : `python -m pip install -r requirements.txt`
5. Lance `demarrer.bat`. La fenêtre doit afficher `AI: Claude (claude-opus-5)`.
6. Ouvre http://localhost:8000 : le badge du chat affiche **« IA Claude »**.

Modèle moins cher si beaucoup de messages : ajoute `INVENTORY_AI_MODEL=claude-sonnet-5` dans `.env`.

## 2. Test chez toi, avant le restaurant (1 soirée)

Avec les données de démo (la base actuelle) :

| Test | À taper / faire | Attendu |
|---|---|---|
| Commande libre | `3 sacs de carottes, 5 steaks et 3 huiles d'olive` | Commande ce qui est clair, pose des questions sur le reste (sacs ?) |
| Question cuisine | `combien de temps se garde le poulet cuit ?` | Réponse normale, courte |
| Hors sujet | `qui a gagné le match hier ?` | Refus poli, ramène à la cuisine |
| Garde-fou budget | `commande 5000 steaks` | Bloqué par le budget |
| Prix | `qui vend le saumon le moins cher ?` | Compare, dit « prix estimé » |
| Facture | Prix → Importer : photo d'une vraie facture | Aperçu avec les lignes et les prix convertis |

Note tout ce qui te paraît faux : je corrigerai les instructions de l'IA.

## 3. Préparer le restaurant (1-2 h avec le chef)

1. Crée la vraie base (l'ancienne est gardée en `.bak`) :
   ```
   python setup_restaurant.py --force
   ```
   Nom, région (QC), couverts/jour, budget d'achat par semaine.
2. **Ingrédients** : les 20-30 produits principaux, avec le stock réel compté ce jour-là.
   Le plus rapide : dire à l'IA « on utilise du saumon, du steak haché, des patates, de la crème… ».
3. **Recettes** : les 5-10 plats qui se vendent le plus (Configuration → Recettes).
4. **Vrais prix** : importe les 3-5 dernières factures de chaque fournisseur (Prix → Importer).
   Vérifie l'aperçu, surtout les lignes « faible » et les unités.

## 4. Installation sur place

- Un PC qui reste allumé pendant le service, sur le Wi-Fi du restaurant, lance `demarrer.bat`.
- Tablette / téléphone en cuisine : ouvre `http://ADRESSE-DU-PC:8000` (l'adresse s'affiche au
  démarrage). Tape le code d'accès une fois. Windows peut demander d'autoriser Python sur le
  réseau privé : accepte (réseau **privé** seulement).
- Utilise le Wi-Fi du personnel, pas le Wi-Fi invités.
- Une sauvegarde de la base est créée dans `backups\` à chaque démarrage. Copie ce dossier
  sur une clé USB ou OneDrive chaque semaine.

## 5. Le pilote : 4 semaines

**Important : les commandes ne sont PAS envoyées aux fournisseurs** (aucun email, aucun lien
avec Sysco/GFS). Le système propose et enregistre ; le chef continue de commander comme d'habitude.

- **Semaine 1 — en parallèle.** Chaque soir : entrer les ventes du jour (rapport de caisse →
  « on a vendu 40 soupes, 25 burgers… » dans le chat). Chaque livraison : « Reçue ». Chaque
  facture : importer. Comparer ce que l'IA aurait commandé avec ce que le chef a commandé.
- **Fin de semaine 1 : compter le stock physique** et comparer avec l'écran. L'écart dit si
  les recettes (quantités par portion) sont justes. Corriger les recettes.
- **Semaines 2-4.** Même routine. Le chef commence à suivre les propositions de l'IA.

À mesurer (pour vendre ensuite) :
- écart stock écran / stock réel (objectif < 10 %) ;
- temps passé à faire les commandes, avant / après ;
- ruptures de stock et pertes (produits jetés) ;
- économies trouvées grâce à la comparaison des prix.

## 6. Limites actuelles à dire au restaurant

- Commandes non envoyées aux fournisseurs (étape suivante : email ou PDF de bon de commande).
- Pas de comptes utilisateurs : un seul code d'accès partagé. À ne pas mettre sur Internet.
- Ventes saisies à la main (pas encore de lien avec la caisse).
- Prix du catalogue estimés tant qu'aucune facture n'a été importée.
- Budget hebdomadaire non remis à zéro automatiquement le lundi.
- Un seul PC fait tourner le système : s'il s'éteint, les tablettes n'ont plus accès.
