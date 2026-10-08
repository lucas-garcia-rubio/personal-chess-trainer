# Personal Chess Trainer

Aplicativo web local, single-user, que analisa as partidas de xadrez do próprio jogador e destaca seus erros.

## Partidas

**Player**:
O usuário único do aplicativo — a pessoa cujas partidas são analisadas. Sempre existirá exatamente um.
_Avoid_: usuário, conta

**Game**:
Uma partida de xadrez disputada pelo Player, obtida de uma Game Source.
_Avoid_: partida importada, match

**Game Source**:
Provedor externo de onde as Games são obtidas. Hoje apenas Lichess; Chess.com é uma Game Source futura, não uma integração distinta.
_Avoid_: integração, API de xadrez

**Sync**:
A operação que busca as Games novas de uma Game Source e, ao importá-las, produz suas Analyses automaticamente.
_Avoid_: importar, atualizar, baixar

**Import**:
A operação que recebe exatamente uma Game em PGN fornecida pelo Player e produz sua Analysis com um avaliador local.
_Avoid_: Sync manual, sincronização manual

**Instante Operacional**:
O momento em que uma Game foi disputada, derivado dos headers: UTCDate com UTCTime, depois Date à meia-noite UTC, e por fim um fallback seguro quando não há data válida. É o que ordena a lista da Home.
_Avoid_: data de criação, instante da Analysis

## Treino (futuro)

**Trainer**:
O papel de treinador exercido pelo aplicativo: propõe linhas, pede continuações ao Player e comenta suas decisões em tempo real.
_Avoid_: coach, assistente, IA

## Análise

**Analysis**:
O estudo de uma Game centrado nos erros do Player: seus Critical Moments, o melhor lance em cada um, e a curva de avaliação da partida.
_Avoid_: relatório, review, anotações

**Critical Moment**:
Uma posição de uma Game em que o lance do Player foi classificado como Inaccuracy, Mistake ou Blunder.
_Avoid_: erro, momento-chave

**Win%**:
A probabilidade estimada de vitória numa posição, derivada da avaliação da posição.
_Avoid_: chance, expectativa

**Inaccuracy**:
A menos severa das três classificações de lance: queda pequena de Win%.
_Avoid_: imprecisão

**Mistake**:
A classificação intermediária: queda média de Win%.
_Avoid_: equívoco

**Blunder**:
A mais severa das três classificações de lance: queda grande de Win%.
_Avoid_: erro grave, derrota
