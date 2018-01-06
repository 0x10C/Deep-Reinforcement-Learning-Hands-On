#!/usr/bin/env python3
import os
import random
import argparse
import logging
import numpy as np
from tensorboardX import SummaryWriter

from libbots import data, model

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable


DEFAULT_FILE = "data/OpenSubtitles/en/Crime/1994/60_101020_138057_pulp_fiction.xml.gz"
SAVES_DIR = "saves"

BATCH_SIZE = 32
LEARNING_RATE = 1e-3
MAX_EPOCHES = 1000
MAX_TOKENS = 15
GRAD_CLIP = 0.1

log = logging.getLogger("train")

TEACHER_PROB = 0.5
ENTROPY_BETA = 0.0
LEARNING_RATE_RL = 1e-3


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)-15s %(levelname)s %(message)s", level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--cornell", help="Use Cornell Movie Dialogues database could be a category or empty string to load full data")
    parser.add_argument("--data", default=DEFAULT_FILE, help="Could be file name to load or category dir")
    parser.add_argument("--cuda", action='store_true', default=False, help="Enable cuda")
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    args = parser.parse_args()

    saves_path = os.path.join(SAVES_DIR, args.name)
    os.makedirs(saves_path, exist_ok=True)

    phrase_pairs, phrase_pairs_dict = data.load_data(args, MAX_TOKENS)
    log.info("Obtained %d phrase pairs with %d uniq words", len(phrase_pairs), len(phrase_pairs_dict))
    emb_dict, emb = data.read_embeddings(phrase_pairs_dict)
    train_data = data.encode_phrase_pairs(phrase_pairs, emb_dict)
    log.info("Training data converted, got %d samples", len(train_data))

    data.save_embeddings(saves_path, emb_dict, emb)

    # initialize embedding lookup table
    embeddings = nn.Embedding(num_embeddings=emb.shape[0], embedding_dim=emb.shape[1])
    embeddings.weight.data.copy_(torch.from_numpy(emb))
    embeddings.weight.requires_grad = False
    if args.cuda:
        embeddings.cuda()

    net = model.PhraseModel(emb_size=emb.shape[1], dict_size=emb.shape[0], hid_size=model.HIDDEN_STATE_SIZE)
    if args.cuda:
        net.cuda()
    log.info("Model: %s", net)

    writer = SummaryWriter(comment="-" + args.name)

    if args.load:
        net.load_state_dict(torch.load(args.load))
        log.info("Model loaded from %s, continue training in RL mode...", args.load)
    else:
        optimiser = optim.Adam(net.parameters(), lr=LEARNING_RATE)
        best_bleu = None
        for epoch in range(MAX_EPOCHES):
            random.shuffle(train_data)
            losses = []
            bleu_sum = 0.0
            bleu_count = 0
            for batch in data.iterate_batches(train_data, BATCH_SIZE):
                optimiser.zero_grad()
                input_seq, out_seq_list, out_idx = model.pack_batch(batch, embeddings, cuda=args.cuda)
                enc = net.encode(input_seq)

                net_results = []
                net_targets = []
                for idx, out_seq in enumerate(out_seq_list):
                    ref_indices = out_idx[idx][1:]
                    if random.random() < TEACHER_PROB:
                        r = net.decode_teacher(net.get_encoded_item(enc, idx), out_seq)
                    else:
                        r, _ = net.decode_chain_argmax(embeddings, net.get_encoded_item(enc, idx),
                                                       out_seq.data[0], len(ref_indices))
                    net_results.append(r)
                    net_targets.extend(ref_indices)
                    bleu_sum += model.seq_bleu(r, ref_indices)
                    bleu_count += 1
                results_v = torch.cat(net_results)
                targets_v = Variable(torch.LongTensor(net_targets))
                if args.cuda:
                    targets_v = targets_v.cuda()
                loss_v = F.cross_entropy(results_v, targets_v)
                loss_v.backward()
                optimiser.step()

                losses.append(loss_v.data.cpu().numpy()[0])
            bleu = bleu_sum / bleu_count
            log.info("Epoch %d: mean loss %.3f, mean BLEU %.3f", epoch, np.mean(losses), bleu)
            writer.add_scalar("loss", np.mean(losses), epoch)
            writer.add_scalar("bleu", bleu, epoch)
            if best_bleu is None or best_bleu < bleu:
                if best_bleu is not None:
                    torch.save(net.state_dict(), os.path.join(saves_path, "pre_bleu_%.3f_%02d.dat" % (bleu, epoch)))
                    log.info("Best BLEU updated %.3f", bleu)
                best_bleu = bleu

    writer.close()
