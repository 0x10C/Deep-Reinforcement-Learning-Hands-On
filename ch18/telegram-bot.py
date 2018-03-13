#!/usr/bin/env python3
# This module requires python-telegram-bot
import os
import sys
import glob
import random
import logging
import numpy as np
import configparser
import argparse

from lib import game, model

try:
    import telegram.ext
    from telegram.error import TimedOut
except ImportError:
    print("You need python-telegram-bot package installed to start the bot")
    sys.exit()

import torch
import torch.nn.functional as F

# Configuration file with the following contents
# [telegram]
# api=API_KEY
CONFIG_DEFAULT = "~/.config/rl_ch18_bot.ini"

log = logging.getLogger("telegram")


class Session:
    BOT_PLAYER = game.PLAYER_BLACK
    USER_PLAYER = game.PLAYER_WHITE

    def __init__(self, model_file):
        self.model = model.Net(input_shape=model.OBS_SHAPE, actions_n=game.GAME_COLS)
        self.model.load_state_dict(torch.load(model_file, map_location=lambda storage, loc: storage))
        self.state = game.INITIAL_STATE
        self.value = None

    def move_player(self, col):
        self.state, won = game.move(self.state, col, self.USER_PLAYER)
        return won

    def move_bot(self):
        state_list = game.decode_binary(self.state)
        batch_v = model.state_lists_to_batch([state_list], [self.BOT_PLAYER])
        logits_v, values_v = self.model(batch_v)
        probs_v = F.softmax(logits_v)
        probs = probs_v[0].data.cpu().numpy()
        self.value = values_v.data.cpu().numpy()[0]
        while True:
            action = np.random.choice(game.GAME_COLS, p=probs)
            if action in game.possible_moves(self.state):
                break
        self.state, won = game.move(self.state, action, self.BOT_PLAYER)
        return won

    def is_valid_move(self, move_col):
        return move_col in game.possible_moves(self.state)

    def render(self):
        l = game.render(self.state)
        l = "\n".join(l)
        l = l.replace("0", 'O').replace("1", "X")
        board = "0123456\n-------\n" + l + "\n-------\n0123456"
        extra = ""
        if self.value is not None:
            extra = "Position evaluation: %.2f\n" % float(self.value)
        return extra + "<pre>%s</pre>" % board



class PlayerBot:
    def __init__(self, models_dir):
        self.sessions = {}
        self.models = self._read_models(models_dir)

    def _read_models(self, models_dir):
        result = {}
        for idx, name in enumerate(sorted(glob.glob(os.path.join(models_dir, "*.dat")))):
            result[idx] = name
        return result

    def command_help(self, bot, update):
        bot.send_message(chat_id=update.message.chat_id, parse_mode="HTML", disable_web_page_preview=True,
                         text="""
This a <a href="https://en.wikipedia.org/wiki/Connect_Four">4-in-a-row</a> game bot trained with AlphaGo Zero method for the <a href="https://www.packtpub.com/big-data-and-business-intelligence/practical-deep-reinforcement-learning">Practical Deep Reinforcement Learning</a> book. 

<b>Welcome!</b>

This bot understands the following commands:
<b>/list</b> to list available pre-trained models (the higher the ID, the stronger the play)
<b>/play MODEL_ID</b> to start the new game against the specified model

During the game, your moves are numbers of columns to drop the disk.
""")


    def command_list(self, bot, update):
        if len(self.models) == 0:
            reply = ["There are no models currently available, sorry!"]
        else:
            reply = ["The list of available models with their IDs"]
            for idx, name in sorted(self.models.items()):
                reply.append("<b>%d</b>: %s" % (idx, os.path.basename(name)))

        bot.send_message(chat_id=update.message.chat_id, text="\n".join(reply), parse_mode="HTML")

    def command_play(self, bot, update, args):
        chat_id = update.message.chat_id
        try:
            model_id = int(args[0])
        except ValueError:
            bot.send_message(chat_id=chat_id, text="Wrong argumants! Use '/play <MODEL_ID>, to start the game")
            return

        if model_id not in self.models:
            bot.send_message(chat_id=chat_id, text="There is no such model, use /list command to get list of IDs")
            return

        if chat_id in self.sessions:
            bot.send_message(chat_id=chat_id, text="You already have the game in progress, it will be discarded")
            del self.sessions[chat_id]

        self.sessions[chat_id] = Session(self.models[model_id])
        player_moves = random.choice([False, True])
        if player_moves:
            bot.send_message(chat_id=chat_id, text="Your move is first (you're playing with O), please give the column to put your checker with /move [0-6]")
        else:
            bot.send_message(chat_id=chat_id, text="The first move is mine (I'm playing with X), moving...")
            self.sessions[chat_id].move_bot()
        bot.send_message(chat_id=chat_id, text=self.sessions[chat_id].render(), parse_mode="HTML")

    def text(self, bot, update):
        chat_id = update.message.chat_id

        if chat_id not in self.sessions:
            bot.send_message(chat_id=chat_id, text="You have no game in progress. Start it with <b>/play MODEL_ID</b> "
                                                   "(or use <b>/help</b> to see the list of commands)",
                             parse_mode='HTML')
            return

        try:
            move_col = int(update.message.text)
        except ValueError:
            bot.send_message(chat_id=chat_id, text="I don't understand. In play mode you can give a number "
                                                   "from 0 to 6 to specify your move.")
            return

        if move_col < 0 or move_col > 6:
            bot.send_message(chat_id=chat_id, text="Wrong column specified! It must be in range 0-6")
            return

        if not self.sessions[chat_id].is_valid_move(move_col):
            bot.send_message(chat_id=chat_id, text="Move %d is invalid!" % move_col)
            return

        won = self.sessions[chat_id].move_player(move_col)
        if won:
            bot.send_message(chat_id=chat_id, text="You won! Congratulations!")
            del self.sessions[chat_id]
            return

        won = self.sessions[chat_id].move_bot()
        bot.send_message(chat_id=chat_id, text=self.sessions[chat_id].render(), parse_mode="HTML")

        if won:
            bot.send_message(chat_id=chat_id, text="I won! Wheeee!")
            del self.sessions[chat_id]

    def error(self, bot, update, error):
        try:
            raise error
        except TimedOut:
            log.info("Timed out error")


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)-15s %(levelname)s %(message)s", level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=CONFIG_DEFAULT,
                        help="Configuration file for the bot, default=" + CONFIG_DEFAULT)
    parser.add_argument("-m", "--models", required=True, help="Directory name with models to serve")
    prog_args = parser.parse_args()

    conf = configparser.ConfigParser()
    if not conf.read(os.path.expanduser(prog_args.config)):
        log.error("Configuration file %s not found", prog_args.config)
        sys.exit()

    player_bot = PlayerBot(prog_args.models)

    updater = telegram.ext.Updater(conf['telegram']['api'])
    updater.dispatcher.add_handler(telegram.ext.CommandHandler('help', player_bot.command_help))
    updater.dispatcher.add_handler(telegram.ext.CommandHandler('list', player_bot.command_list))
    updater.dispatcher.add_handler(telegram.ext.CommandHandler('play', player_bot.command_play, pass_args=True))
    updater.dispatcher.add_handler(telegram.ext.MessageHandler(telegram.ext.Filters.text, player_bot.text))
    updater.dispatcher.add_error_handler(player_bot.error)

    log.info("Bot initialized, started serving")
    updater.start_polling()
    updater.idle()

    pass
