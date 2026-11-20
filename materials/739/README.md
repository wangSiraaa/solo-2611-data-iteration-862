# 739 合成位型与参考

input.json为题定32位格式的原件，不通过宿主float生成或改写。publish_reference表示复制旧完整版本、替换所列项目后发布新完整参考，不丢掉未列项目。NaN的payload为23位fraction原值，其余分类的payload可明确为不适用。

r1:s1差1/671088640，OUTSIDE；s2数值0、sign=1，PASS；s3/s4均NONFINITE但类别不同，s3 fraction为4194305；s5为精确1/2^149，零容差为OUTSIDE。更正后s1为1/8且r2参考也是1/8，PASS；旧报告仍保留原bits/差值。没有位型解释实现或参考核对程序。
