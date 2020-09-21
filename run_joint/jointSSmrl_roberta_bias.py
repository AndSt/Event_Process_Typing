import torch
print(torch.cuda.is_available())
from transformers import RobertaTokenizer, RobertaModel, GPT2Model, RobertaForMultipleChoice
import tqdm, sklearn
import numpy as np
import os, time, sys
import pickle
import multiprocessing
from multiprocessing import Process, Value, Manager
from itertools import chain
import scipy, random
import heapq

if '../utils' not in sys.path:
    sys.path.append('../utils')

from data import Data


import nltk
nltk.download('wordnet')
from nltk.corpus import wordnet as wn
def get_hypernym_path(input_word, max_length=20, return_single_set = True):
    paths = list()
    syn_sets = wn.synsets(input_word)
    for syn in syn_sets:
        raw_path = syn.hypernym_paths()
        for p in raw_path:
            tmp_path = [input_word]
            last_node = input_word
            for tmp_synset in p[::-1]:
                tmp_postag = tmp_synset._name.split('.')[1]
                if tmp_postag == 'v':
                    new_node = tmp_synset._name
                else:
                    new_node = tmp_synset._name
                dot_p = new_node.find('.')
                if dot_p > 0:
                    new_node = new_node[:dot_p]
                tmp_path.append(new_node)
                last_node = new_node
            paths.append(tmp_path[:max_length])
    if len(paths) == 0:
        paths = [[input_word]]
    if return_single_set:
        sets = set([])
        for x in paths:
            for y in x:
                sets.add(y)
            return sets
    return paths


class torchpart(object):

    def __init__(self):
        self.model=None
        self.tokenizer=None
        self.batch_size=64
        self.epsilon = 1e-8
        self.epoch=1
        self._v_vec, self._a_vec = None, None#Serve purpose
    
    def initialize(self, pretrained='roberta-base', tokenizer='roberta-base', sep_token='</s>', this_margin=0.2, this_margin2=0.2):
        self.tokenizer = RobertaTokenizer.from_pretrained(tokenizer, sep_token=sep_token)
        self.model = RobertaModel.from_pretrained(pretrained, output_hidden_states=True)
        self.model.cuda()
        #self._M2 = torch.nn.Linear(768*2, 768).cuda()
        #self._M = torch.nn.Linear(768*2, 1).cuda()
        self._Mv = torch.nn.Linear(768, 768, bias=True).cuda()
        self._Mv.weight.data.copy_(torch.eye(768))
        self._Mv.bias.data.copy_(torch.zeros(768))
        self._Ma = torch.nn.Linear(768, 768, bias=True).cuda()
        self._Ma.weight.data.copy_(torch.eye(768))
        self._Ma.bias.data.copy_(torch.zeros(768))
        self._loss = torch.nn.MarginRankingLoss(margin=this_margin).cuda()
        self._loss2 = torch.nn.MarginRankingLoss(margin=this_margin2).cuda()
        self.bos_token = '[CLS] '
        self.sep_token = ' ' + sep_token + ' '
    
    # sequences are already with [SEP], and partitioned into training sets.
    def train_joint(self, verbs, args, sequences, true_senses, true_arg_senses, epochs=5, learning_rate=0.01, alpha=1.):
        #all_cases = [[self.sep_token.join([s, v]) for v in verbs] for s in sequences]
        all_cases = [s for s in sequences]
        true_senses = [x for x in true_senses]
        true_arg_senses = [x for x in true_arg_senses]
        assert (len(all_cases) == len(true_senses))
        print ("Begin training with ", len(all_cases), " cases and ", len(verbs), "verb choices and ", len(args), "arg choices.")
        
        #params = [x for x in self.model.parameters()] + [self._M]
        
        optimizer = torch.optim.Adam(chain(self.model.parameters(), self._Mv.parameters(), self._Ma.parameters()), lr=learning_rate, amsgrad=True)#torch.optim.Adam(chain(self.model.parameters(), self._M.parameters()), lr=learning_rate)
        
        indicator = torch.tensor(np.ones(self.batch_size, dtype=np.float32), requires_grad=False).cuda()
        cosine = torch.nn.CosineSimilarity(dim=1).cuda()
        
        for epoch in range(epochs):
            print ("Begin epoch #", self.epoch)
            l = len(all_cases)
            indices = np.arange(l)
            np.random.shuffle(indices)
            this_cases, this_verbs, this_args = [all_cases[i] for i in np.concatenate([indices, indices[0:self.batch_size]])], [true_senses[i] for i in np.concatenate([indices, indices[0:self.batch_size]])], [true_arg_senses[i] for i in np.concatenate([indices, indices[0:self.batch_size]])]
            step = random.randint(1, len(this_verbs) - 1)
            false_verbs, false_args = [], []
            for i in tqdm.tqdm(range(len(this_verbs))):
                this_step = step
                while this_verbs[(i + this_step) % len(this_verbs)][:this_verbs[(i + this_step) % len(this_verbs)].find(' ')] == this_verbs[i][:this_verbs[i].find(' ')]:
                    this_step += 1
                false_verbs.append(this_verbs[(i + this_step) % len(this_verbs)])
                false_args.append(this_args[(i + this_step) % len(this_verbs)])
            
            
            this_loss = []
            for b in tqdm.tqdm(range(0, l, self.batch_size)):
                batch_cases, batch_verbs, batch_false = [this_cases[i] for i in range(b, b + self.batch_size)], [this_verbs[i] for i in range(b, b + self.batch_size)], [false_verbs[i] for i in range(b, b + self.batch_size)]
                batch_args, batch_false_args = [this_args[i] for i in range(b, b + self.batch_size)], [false_args[i] for i in range(b, b + self.batch_size)]
                
                input_ids = torch.tensor([self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True) for ss in batch_cases]).cuda()

                input_verbs = torch.tensor([self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True) for ss in batch_verbs]).cuda()
                
                input_false = torch.tensor([self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True) for ss in batch_false]).cuda()
                
                input_args = torch.tensor([self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True) for ss in batch_args]).cuda()
                
                input_false_args = torch.tensor([self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True) for ss in batch_false_args]).cuda()

                outputs = torch.mean(self.model(input_ids)[0], 1)
                output_verbs = torch.mean(self.model(input_verbs)[0], 1)
                output_false = torch.mean(self.model(input_false)[0], 1)
                output_args = torch.mean(self.model(input_args)[0], 1)
                output_false_args = torch.mean(self.model(input_false_args)[0], 1)
                #print ([x.shape for x in self.model(input_ids)])
                #print (outputs.shape, output_verbs.shape)
                
                #loss1 = self._M(torch.sub(outputs, output_verbs).abs()).squeeze()
                cos1 = cosine(self._Mv(outputs), output_verbs)
                cos2 = cosine(self._Mv(outputs), output_false)
                
                loss1 = self._loss(cos1, cos2, indicator)
                
                cos3 = cosine(self._Ma(outputs), output_args)
                cos4 = cosine(self._Ma(outputs), output_false_args)
                
                loss2 = self._loss2(cos3, cos4, indicator)
                
                loss = loss1 + alpha * loss2
                
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                
                this_loss.append(loss.data.cpu().numpy())
            
            this_loss = np.average(this_loss)
            print ("Loss = ", this_loss)
            self.epoch += 1
            if np.isnan(this_loss):
                exit()
                
    def test_verb(self, verbs, sequences, true_ids, v2s, limit_ids=True):
        cand_ids = set([])
        if limit_ids:
            for id in true_ids:
                cand_ids.add(id)
        else:
            cand_ids = set([i for i in range(len(verbs)) if v2s.get(verbs[i]) is not None])
        all_cases = [s for s in sequences]
        senses = [v2s[v] if v2s.get(v) is not None else ' ' for v in verbs]
        #true_verbs = [verbs[x] for x in true_ids]
        assert (len(all_cases) == len(true_ids))
        print ("Begin testing with ", len(all_cases), " case.")
        
        cpu_count = multiprocessing.cpu_count()
        manager = Manager()
        mrr, hits1, hits10 = manager.list(), manager.list(), manager.list()
        
        index = Value('i',0,lock=True)
        
        #self._M.cpu()
        #self.model.cpu()
        
        s_vec = np.array([torch.mean(self.model(torch.tensor(self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0] for ss in tqdm.tqdm(all_cases)])
        v_vec = np.array([torch.mean(self.model(torch.tensor(self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0] for ss in tqdm.tqdm(senses)])
        print (s_vec.shape, v_vec.shape)
        
        self._Mv.cpu()
        W1, b1 = self._Mv.weight.data.numpy(), self._Mv.bias.data.numpy()
        self._Mv.cuda()
        

        t0 = time.time()
        def test(s_vec, v_vec, cand_ids):
            while index.value < len(all_cases):
                id = index.value
                index.value += 1
                if id < 10 or id % int(len(all_cases) / 50) == 0:
                    print ('At ',id)
                if id >= len(all_cases):
                    return
                #if id % 1000 == 0:
                    #print (id ,'/', len(all_cases), ' time used ',time.time() - t0)
                tid = true_ids[id]
                this_s, this_v = np.dot(W1, s_vec[id]) + b1, v_vec[tid]
                t_dist = scipy.spatial.distance.cosine(this_s, this_v)
                rank = 1
                for i in cand_ids:
                    if i != tid and scipy.spatial.distance.cosine(this_s, v_vec[i]) + self.epsilon < t_dist:
                        rank +=1
                h1, h10 = 0., 0.
                if rank < 11:
                    h10 = 1.
                    if rank < 2:
                        h1 = 1.
                mrr.append(1. / rank)
                hits1.append(h1)
                hits10.append(h10)
        
        processes = [Process(target=test, args=(s_vec, v_vec, cand_ids)) for x in range(16)]
        
        for p in processes:
            p.start()
        for p in processes:
            p.join()
        
        mrr, hits1, hits10 = np.average(mrr), np.average(hits1), np.average(hits10)
        print (mrr, hits1, hits10)
        return mrr, hits1, hits10

    def test_arg(self, args, sequences, true_ids, a2s, limit_ids=True):
        cand_ids = set([])
        if limit_ids:
            for id in true_ids:
                cand_ids.add(id)
        else:
            cand_ids = set([i for i in range(len(args)) if a2s.get(args[i]) is not None])
        all_cases = [s for s in sequences]
        senses = [a2s[v] if a2s.get(v) is not None else ' ' for v in args]
        #true_args = [args[x] for x in true_ids]
        assert (len(all_cases) == len(true_ids))
        print ("Begin testing with ", len(all_cases), " case.")
        
        cpu_count = multiprocessing.cpu_count()
        manager = Manager()
        mrr, hits1, hits10 = manager.list(), manager.list(), manager.list()
        
        index = Value('i',0,lock=True)
        
        s_vec = np.array([torch.mean(self.model(torch.tensor(self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0] for ss in tqdm.tqdm(all_cases)])
        v_vec = np.array([torch.mean(self.model(torch.tensor(self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0] for ss in tqdm.tqdm(senses)])
        print (s_vec.shape, v_vec.shape)
        
        self._Ma.cpu()
        W1, b1 = self._Ma.weight.data.numpy(), self._Ma.bias.data.numpy()
        self._Ma.cuda()
        

        t0 = time.time()
        def test(s_vec, v_vec, cand_ids):
            while index.value < len(all_cases):
                id = index.value
                index.value += 1
                if id < 10 or id % int(len(all_cases) / 50) == 0:
                    print ('At ',id)
                if id >= len(all_cases):
                    return
                #if id % 1000 == 0:
                    #print (id ,'/', len(all_cases), ' time used ',time.time() - t0)
                tid = true_ids[id]
                this_s, this_v = np.dot(W1, s_vec[id]) + b1, v_vec[tid]
                t_dist = scipy.spatial.distance.cosine(this_s, this_v)
                rank = 1
                for i in cand_ids:
                    if i != tid and scipy.spatial.distance.cosine(this_s, v_vec[i]) + self.epsilon < t_dist:
                        rank +=1
                h1, h10 = 0., 0.
                if rank < 11:
                    h10 = 1.
                    if rank < 2:
                        h1 = 1.
                mrr.append(1. / rank)
                hits1.append(h1)
                hits10.append(h10)
        
        processes = [Process(target=test, args=(s_vec, v_vec, cand_ids)) for x in range(16)]
        
        for p in processes:
            p.start()
        for p in processes:
            p.join()
        
        mrr, hits1, hits10 = np.average(mrr), np.average(hits1), np.average(hits10)
        print (mrr, hits1, hits10)
        return mrr, hits1, hits10
    
    def profile_test_verb(self, verbs, sequences, seq_len, true_ids, v2s, verb_thres=50, length_thres=5, silent=True):
        cand_ids = set([])
        id_count = {}
        for id in true_ids:
            cand_ids.add(id)
            if id_count.get(id) is None:
                id_count[id] = 1
            else:
                id_count[id] += 1
        print (len(cand_ids))
        id_count = [(k,v) for k,v in id_count.items()]
        id_count.sort(key = lambda x: x[1], reverse=True)
        upper_id = set([x[0] for x in id_count[:verb_thres]])
        all_cases = [s for s in sequences]
        senses = [v2s[v] if v2s.get(v) is not None else ' ' for v in verbs]
        #true_verbs = [verbs[x] for x in true_ids]
        assert (len(all_cases) == len(true_ids))
        print ("Begin testing with ", len(all_cases), " case.")
        
        cpu_count = multiprocessing.cpu_count()
        manager = Manager()
        mrr_vl, hits1_vl, hits10_vl = manager.list(), manager.list(), manager.list()
        mrr_vu, hits1_vu, hits10_vu = manager.list(), manager.list(), manager.list()
        mrr_ll, hits1_ll, hits10_ll = manager.list(), manager.list(), manager.list()
        mrr_lu, hits1_lu, hits10_lu = manager.list(), manager.list(), manager.list()
        
        
        index = Value('i',0,lock=True)
        
        #self._M.cpu()
        #self.model.cpu()
        
        s_vec = np.array([torch.mean(self.model(torch.tensor(self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0] for ss in tqdm.tqdm(all_cases)])
        v_vec = np.array([torch.mean(self.model(torch.tensor(self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0] for ss in tqdm.tqdm(senses)])
        print (s_vec.shape, v_vec.shape)
        
        self._Mv.cpu()
        W1, b1 = self._Mv.weight.data.numpy(), self._Mv.bias.data.numpy()
        self._Mv.cuda()
        

        t0 = time.time()
        def test(s_vec, v_vec, cand_ids):
            while index.value < len(all_cases):
                id = index.value
                index.value += 1
                if (id < 10 or id % int(len(all_cases) / 50) == 0) and (not silent):
                    print ('At ',id)
                if id >= len(all_cases):
                    return
                #if id % 1000 == 0:
                    #print (id ,'/', len(all_cases), ' time used ',time.time() - t0)
                tid = true_ids[id]
                this_s, this_v = np.dot(W1, s_vec[id]) + b1, v_vec[tid]
                t_dist = scipy.spatial.distance.cosine(this_s, this_v)
                rank = 1
                for i in cand_ids:
                    if i != tid and scipy.spatial.distance.cosine(this_s, v_vec[i]) + self.epsilon < t_dist:
                        rank +=1
                h1, h10 = 0., 0.
                if rank < 11:
                    h10 = 1.
                    if rank < 2:
                        h1 = 1.
                if tid in upper_id:
                    mrr_vu.append(1. / rank)
                    hits1_vu.append(h1)
                    hits10_vu.append(h10)
                else:
                    mrr_vl.append(1. / rank)
                    hits1_vl.append(h1)
                    hits10_vl.append(h10)
                if seq_len[id] >= length_thres:
                    mrr_lu.append(1. / rank)
                    hits1_lu.append(h1)
                    hits10_lu.append(h10)
                else:
                    mrr_ll.append(1. / rank)
                    hits1_ll.append(h1)
                    hits10_ll.append(h10)
                #mrr.append(1. / rank)
                #hits1.append(h1)
                #hits10.append(h10)
                
        
        processes = [Process(target=test, args=(s_vec, v_vec, cand_ids)) for x in range(16)]
        
        for p in processes:
            p.start()
        for p in processes:
            p.join()
        
        mrr_vu, hits1_vu, hits10_vu = np.average(mrr_vu), np.average(hits1_vu), np.average(hits10_vu)
        mrr_vl, hits1_vl, hits10_vl = np.average(mrr_vl), np.average(hits1_vl), np.average(hits10_vl)
        mrr_lu, hits1_lu, hits10_lu = np.average(mrr_lu), np.average(hits1_lu), np.average(hits10_lu)
        mrr_ll, hits1_ll, hits10_ll = np.average(mrr_ll), np.average(hits1_ll), np.average(hits10_ll)
        
        print ("v top", verb_thres, ':', mrr_vu, hits1_vu, hits10_vu)
        print ("v lesser", ':', mrr_vl, hits1_vl, hits10_vl)
        print ("l_count<=", length_thres, ':', mrr_ll, hits1_ll, hits10_ll)
        print ("l_count>", length_thres, ':', mrr_lu, hits1_lu, hits10_lu)
        #return mrr, hits1, hits10
    
    def serve_verb(self, sequence, data, limit_ids=None, topk=3, return_emb=False):
        r_verbs = {y: x for x, y in data.verb_vocab.items()}
        n_verbs = len([x for x, y in data.verb_vocab.items()])
        verbs = [r_verbs[x] for x in range(n_verbs)]
        sequence = data.join_batch_sent([sequence], begin='<s> ', sep=' </s> ')[0]
        
        cand_ids = set([])
        if limit_ids is not None:
            for id in limit_ids:
                cand_ids.add(id)
        else:
            cand_ids = set([i for i in range(n_verbs) if data.v2s.get(verbs[i]) is not None])
        senses = [data.v2s[v] if data.v2s.get(v) is not None else ' ' for v in verbs]
        
        s_vec = torch.mean(self.model(torch.tensor(self.tokenizer.encode(sequence, add_special_tokens=True, max_length=50, pad_to_max_length=True)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0]
        
        #print ('SC0:', sequence)
        #print ('SC1:', self.tokenizer.encode(sequence, add_special_tokens=True, max_length=50, pad_to_max_length=True)[:9])
        #print ('SC2:', s_vec[:3])
        
        if self._v_vec is None:
            self._v_vec = v_vec = np.array([torch.mean(self.model(torch.tensor(self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0] for ss in tqdm.tqdm(senses)])
        else:
            v_vec = self._v_vec
        #print (s_vec.shape, v_vec.shape)
        
        self._Mv.cpu()
        W1, b1 = self._Mv.weight.data.numpy(), self._Mv.bias.data.numpy()
        self._Mv.cuda()
        
        
        this_s = np.dot(W1, s_vec) + b1
        rst = []
        
        for i in cand_ids:
            t_dist = scipy.spatial.distance.cosine(this_s, v_vec[i])
            if return_emb:
                rst.append((r_verbs[i], t_dist, v_vec[i]))
            else:
                rst.append((r_verbs[i], t_dist))
        
        return heapq.nsmallest(topk, rst, key = lambda x: x[1])
        
    def serve_arg(self, sequence, data, limit_ids=None, topk=3, return_emb=False):
        r_args = {y: x for x, y in data.arg_vocab.items()}
        n_args = len([x for x, y in data.arg_vocab.items()])
        args = [r_args[x] for x in range(n_args)]
        sequence = data.join_batch_sent([sequence], begin='<s> ', sep=' </s> ')[0]
        
        cand_ids = set([])
        if limit_ids is not None:
            for id in limit_ids:
                cand_ids.add(id)
        else:
            cand_ids = set([i for i in range(n_args) if data.a2s.get(args[i]) is not None and data.a2s.get(args[i]) != data.a2s.get('default')])
        senses = [data.a2s[v] if data.a2s.get(v) is not None else ' ' for v in args]
        
        s_vec = torch.mean(self.model(torch.tensor(self.tokenizer.encode(sequence, add_special_tokens=True, max_length=50, pad_to_max_length=True)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0]
        if self._a_vec is None:
            self._a_vec = v_vec = np.array([torch.mean(self.model(torch.tensor(self.tokenizer.encode(ss, add_special_tokens=True, max_length=50, pad_to_max_length=True)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0] for ss in tqdm.tqdm(senses)])
        else:
            v_vec = self._a_vec
        #print (s_vec.shape, v_vec.shape)
        
        self._Ma.cpu()
        W1, b1 = self._Ma.weight.data.numpy(), self._Ma.bias.data.numpy()
        self._Ma.cuda()
        
        
        this_s = np.dot(W1, s_vec) + b1
        rst = []
        
        for i in cand_ids:
            t_dist = scipy.spatial.distance.cosine(this_s, v_vec[i])
            if return_emb:
                rst.append((r_args[i], t_dist, v_vec[i]))
            else:
                rst.append((r_args[i], t_dist))
        
        return heapq.nsmallest(topk, rst, key = lambda x: x[1])
    
    
    def encode_batch_labels(self, labels):
        #labels is a list of strings
        s_vecs = np.array([torch.mean(self.model(torch.tensor(self.tokenizer.encode(ss, add_special_tokens=False)).cuda().unsqueeze(0))[0], -2).data.cpu().numpy()[0] for ss in tqdm.tqdm(labels)])
        return s_vecs
    
        
    def save(self, filename):
        self.predictor = None
        f = open(filename,'wb')
        pickle.dump(self.__dict__, f, pickle.HIGHEST_PROTOCOL)
        f.close()
        print("Save data object as", filename)
    
    def load(self, filename):
        f = open(filename,'rb')
        tmp_dict = pickle.load(f)
        self.__dict__.update(tmp_dict)
        print("Loaded data object from", filename)


def main():
    this_alpha, this_margin, this_margin2 = 1., 0.2, 0.2
    skip_training = False
    if len(sys.argv) > 1:
        skip_training, this_alpha, this_margin, this_margin2 = bool(int(sys.argv[1])), float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
        #print (sys.argv[1], skip_training)
    try:
        os.mkdir('./seqSSmrl_subsrl')
        os.mkdir('./seqSSmrl_subsrl/RobertaVerbMC/')
    except:
        pass
    data_file, data_bin, model_bin, rst_file = '../data/wikihow_process/wikiHowSubsequences.tsv', '../data/wikihow_process/data_subsrl_1sv_1sa_argtrim.bin', './seqSSmrl_subsrl/RobertaVerbMC/', './seqSSmrl_subsrl/results_subsrl_1sv_1sa_a' + str(this_alpha) + '_m' + str(this_margin) + '_m' + str(this_margin2) + '_wbias_' + time.strftime('%H_%d-%m-%y') + '.txt'
    data = Data()
    if os.path.exists(data_bin):
        data.load(data_bin)
        print ("==ATTN== ",len(data.processes)," sequences.")
    else:
        data.load_tsv_plain(data_file)
        data.save(data_bin)
    
    # W/O n-1 gram
    sequences = data.join_batch_sent(data.processes, begin='<s> ', sep=' </s> ')
    r_verbs = {y: x for x, y in data.verb_vocab.items()}
    n_verbs = len([x for x, y in data.verb_vocab.items()])
    #print (n_verbs)
    verbs = [r_verbs[x] for x in range(n_verbs)]
    vid = np.array(data.verb_id)
    true_senses = [data.v2s[verbs[x]] for x in vid]
    
    r_args = {y: x for x, y in data.arg_vocab.items()}
    n_args = len([x for x, y in data.arg_vocab.items()])
    #print (n_args)
    args = [r_args[x] for x in range(n_args)]
    aid = np.array(data.arg_id)
    true_arg_senses = [data.a2s[args[x]] for x in aid]
    
    #print (true_senses[:3])
    indices = np.arange(len(sequences))
    
    max_fold = 1
    rs = sklearn.model_selection.ShuffleSplit(n_splits=max_fold, test_size=0.1, random_state=777)
    
    avg_mrr, avg_hits1, avg_hits10 = [], [], []
    avg_mrra, avg_hits1a, avg_hits10a = [], [], []
    print (len(verbs), len(args))
    
    with open(rst_file, 'w') as fp:
        fp.write('Total processes ' + str(len(sequences)) + '; verbs ' + str(len(verbs)) + '  ; args ' + str(len(args)) + '\n\n')
        fold = 1
        for train_index, test_index in rs.split(indices):
            train_seq, test_seq = [sequences[x] for x in train_index], [sequences[x] for x in test_index]
            train_senses, test_vid = [true_senses[x] for x in train_index], vid[test_index]
            train_arg_senses, test_aid = [true_arg_senses[x] for x in train_index], aid[test_index]
            M = torchpart()
            if not skip_training:
                M.initialize(this_margin=this_margin, this_margin2=this_margin2)
                #verbs, args, sequences, true_senses, true_arg_senses
                M.train_joint(verbs, args, train_seq, train_senses, train_arg_senses, epochs=50, learning_rate=0.00005, alpha=this_alpha)
            else:
                M.load('./seqSSmrl_subsrl/RobertaVerbMC/tmp_fold'+str(fold)+'.bin')
            #verbs, sequences, true_ids, v2s, limit_ids
            mrr, hits1, hits10 = M.test_verb(verbs, test_seq, test_vid, data.v2s, limit_ids=True)
            fp.write("Fold "+str(fold) + ' epochs ' + str(M.epoch) + '\nVerb mrr=' + str(mrr) + '  hits@1=' + str(hits1) + '  hits@10=' + str(hits10) + '\n')
            mrra, hits1a, hits10a = M.test_arg(args, test_seq, test_aid, data.a2s, limit_ids=True)
            fp.write("Fold "+str(fold) + ' epochs ' + str(M.epoch) + '\nArg mrr=' + str(mrr) + '  hits@1=' + str(hits1) + '  hits@10=' + str(hits10) + '\n')
            M.save('./seqSSmrl_subsrl/RobertaVerbMC/tmp_fold'+'_ep'+str(M.epoch)+'_a'+str(this_alpha) + '_m1-' + str(this_margin) + '_m2-' + str(this_margin2) +'.bin')
            with open('./seqSSmrl_subsrl/RobertaVerbMC/test_fold_'+'_ep'+str(M.epoch)+'_a'+str(this_alpha) + '_m1-' + str(this_margin) + '_m2-' + str(this_margin2) +'.txt', 'w') as fp2:
                for w in test_index:
                    fp2.write(str(w) + '\n')
            
            del M
            avg_mrr.append(mrr)
            avg_hits1.append(hits1)
            avg_hits10.append(hits10)
            avg_mrra.append(mrra)
            avg_hits1a.append(hits1a)
            avg_hits10a.append(hits10a)
            fold += 1
        fp.write('Avg Verb: mrr='+str(np.average(avg_mrr)) + '  hits@1=' + str(np.average(avg_hits1)) + '  hits@10=' + str(np.average(avg_hits10)) + '\n')
        fp.write('Avg Arg: mrr='+str(np.average(avg_mrra)) + '  hits@1=' + str(np.average(avg_hits1a)) + '  hits@10=' + str(np.average(avg_hits10a)) + '\n')

if __name__ == "__main__":
    main()


